"""
Pipeline LangGraph — Orchestration automatique collect → score → rapport.

Utilisation :
    python src/pipeline.py              # exécute le pipeline complet
    python src/pipeline.py --visualiser # affiche le graphe agentique (PNG)

Ce fichier introduit la notion d'agent au sens LangGraph :
- Un état partagé (AgentState) traverse les noeuds
- Les noeuds prennent des décisions (edge conditionnel)
- Le graphe est compilé puis exécuté — on ne "lance pas des scripts", on exécute un agent
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import TypedDict, Optional
from urllib.parse import quote
import email
import imaplib

import anthropic
from bs4 import BeautifulSoup
import httpx
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

import yaml
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from rich.console import Console
from rich.panel import Panel

# Ajouter src/ au path pour les imports relatifs
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection
from collector import (
    charger_config,
    rechercher_offres_cible,
    contient_deal_breaker,
    normaliser_offre,
    sauvegarder_offres,
)
from scorer import scorer_offre, mettre_a_jour_score, formater_profil
from dashboard import exporter_txt


logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")


load_dotenv()

console = Console()


# ─────────────────────────────────────────────
# État partagé du graphe
#
# AgentState est le "fil conducteur" entre les noeuds.
# Chaque noeud reçoit cet état, peut l'enrichir, et le passe au suivant.
# C'est ce qui distingue un agent d'une simple suite de scripts :
# l'état évolue et les décisions en dépendent.
# ─────────────────────────────────────────────

class AgentState(TypedDict):
    new_offers_count: int
    scored_count: int
    report_path: Optional[str]
    excel_path: Optional[str]


# ─────────────────────────────────────────────
# Helpers de scraping (sources externes)
# ─────────────────────────────────────────────

def _filtrer_urls_existantes(offres_db: list[dict], db_path: str) -> list[dict]:
    """
    Retire de offres_db les entrées dont l'URL est déjà présente en base.
    Évite les doublons cross-sources avant même l'INSERT.
    """
    if not offres_db:
        return []
    with get_connection(db_path) as conn:
        nouvelles = []
        for offre in offres_db:
            url = offre.get("url", "")
            if not url:
                continue
            existe = conn.execute(
                "SELECT 1 FROM offres WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
            if not existe:
                nouvelles.append(offre)
    return nouvelles


def _scraper_adzuna(app_id: str, app_key: str, profil: dict, sources: dict, db_path: str) -> int:
    """Collecte Adzuna (logique d'origine). Sauvegarde directement en DB et retourne le count."""
    config_adzuna = sources["adzuna"]
    deal_breakers = profil.get("deal_breakers", [])
    criteres = profil.get("criteres", {})
    mots_cles_defaut = profil["candidat"]["poste_cible"]
    max_par_cible = config_adzuna.get("search_params", {}).get("max_results_par_cible", 25)
    cibles = config_adzuna.get("cibles", [])
    total = 0

    for cible in cibles:
        pays = cible["pays"]
        localisation = cible["localisation"]
        cle_brute = cible.get("mots_cles") or mots_cles_defaut
        liste_mots_cles = [cle_brute] if isinstance(cle_brute, str) else cle_brute

        offres_par_id: dict[str, dict] = {}
        for mot_cle in liste_mots_cles:
            resultats = rechercher_offres_cible(
                app_id, app_key, mot_cle, pays, localisation,
                max_par_cible, criteres, config_adzuna,
            )
            for offre in resultats:
                offres_par_id[offre.get("id", "")] = offre
            time.sleep(1)

        offres_normalisees = [
            normaliser_offre(o, pays)
            for o in offres_par_id.values()
            if not contient_deal_breaker(o, deal_breakers)
        ]
        offres_normalisees = _filtrer_urls_existantes(offres_normalisees, db_path)
        total += sauvegarder_offres(offres_normalisees, db_path)
        time.sleep(1)

    return total


def _scraper_apec(apec_cfg: dict, apify_token: str) -> list[dict]:
    """
    Lance l'actor Apify APEC avec une liste de searchUrls (un par search_term)
    pour contourner la limite de 20 offres par page d'APEC.
    Retourne [] en cas d'erreur (token manquant, timeout, etc.).
    """
    import requests as _req

    if not apify_token:
        logging.warning("APIFY_API_TOKEN manquant — source APEC ignorée")
        return []

    location = apec_cfg.get("location", "France")
    search_terms = apec_cfg.get("search_terms", [])

    # Fallback : si search_terms absent du YAML, liste vide → rien à scraper
    if not search_terms:
        logging.warning("apec.search_terms vide dans sources.yaml — source APEC ignorée")
        return []

    # Construit une URL par terme avec les 4 types de contrat (CDI/CDD/etc.)
    _contrats = (
        "typesConvention=143684&typesConvention=143685"
        "&typesConvention=143686&typesConvention=143687"
    )
    def _build_url(term: str) -> str:
        url = (
            "https://www.apec.fr/candidat/recherche-emploi.html/emploi"
            f"?motsCles={quote(term)}&{_contrats}"
        )
        if location and location.lower() != "france":
            url += f"&lieu={quote(location)}"
        return url

    search_urls = [_build_url(t) for t in search_terms]

    actor_id = os.getenv("APIFY_ACTOR_APEC", "easyapi~apec-jobs-scraper")
    base = "https://api.apify.com/v2"
    headers = {"Content-Type": "application/json"}

    # Lancer le run avec la liste complète de searchUrls
    run_resp = _req.post(
        f"{base}/acts/{actor_id}/runs",
        params={"token": apify_token},
        json={"searchUrls": search_urls, "maxItems": 30},
        headers=headers,
        timeout=30,
    )
    run_resp.raise_for_status()
    run_data = run_resp.json().get("data", {})
    run_id = run_data.get("id")
    dataset_id = run_data.get("defaultDatasetId")

    # Polling jusqu'à SUCCEEDED (max 180 s)
    for _ in range(36):
        time.sleep(5)
        status_resp = _req.get(
            f"{base}/actor-runs/{run_id}",
            params={"token": apify_token},
            timeout=15,
        )
        status_resp.raise_for_status()
        status = status_resp.json().get("data", {}).get("status", "")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            logging.error("Apify run %s terminé avec statut : %s", run_id, status)
            return []

    # Récupérer le dataset
    items_resp = _req.get(
        f"{base}/datasets/{dataset_id}/items",
        params={"token": apify_token},
        timeout=30,
    )
    items_resp.raise_for_status()
    items = items_resp.json()

    CONTRATS_APEC = {
        101887: "CDD",
        101888: "CDI",
        101889: "Interim",
        101890: "Freelance",
        101906: "Alternance",
        597137: "CDI",
    }

    offres = []
    for item in items:
        numero_offre = item.get("numeroOffre", "")
        if not numero_offre:
            continue
        url = (
            f"https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{numero_offre}"
            if numero_offre else ""
        )
        type_contrat = CONTRATS_APEC.get(
            item.get("typeContrat") or item.get("type_contrat"), ""
        )
        offres.append({
            "titre": item.get("intitule", ""),
            "entreprise": item.get("entreprise", {}).get("nom", "") if isinstance(item.get("entreprise"), dict) else item.get("entreprise", ""),
            "localisation": item.get("lieuTexte", ""),
            "description": item.get("texteOffre", ""),
            "url": url,
            "source": "apec",
            "date_publication": item.get("datePublication", ""),
            "salaire": item.get("salaireTexte", ""),
            "type_contrat": type_contrat,
        })
    logging.debug("APEC — %d items reçus, %d normalisés", len(items), len(offres))

    return offres


def _scraper_wttj(wttj_cfg: dict, apify_token: str, search_term: str) -> list[dict]:
    """
    Lance l'actor Apify WTTJ (clearpath~welcome-to-the-jungle-jobs-api).
    Retourne [] en cas d'erreur (token manquant, timeout, etc.).
    """
    import requests as _req

    if not apify_token:
        logging.warning("APIFY_API_TOKEN manquant — source WTTJ ignorée")
        return []

    max_items = wttj_cfg.get("max_items", 30)
    location = wttj_cfg.get("location", "France")
    actor_id = os.getenv("APIFY_ACTOR_WTTJ", "clearpath~welcome-to-the-jungle-jobs-api")
    base = "https://api.apify.com/v2"
    headers = {"Content-Type": "application/json"}

    run_resp = _req.post(
        f"{base}/acts/{actor_id}/runs",
        params={"token": apify_token},
        json={
            "query": search_term,
            "websiteCountry": "fr",
            "location": location,
            "countryCode": "FR",
            "maxItems": max_items,
            "includeDetails": True,
        },
        headers=headers,
        timeout=30,
    )
    run_resp.raise_for_status()
    run_data = run_resp.json().get("data", {})
    run_id = run_data.get("id")
    dataset_id = run_data.get("defaultDatasetId")

    for _ in range(36):
        time.sleep(5)
        status_resp = _req.get(
            f"{base}/actor-runs/{run_id}",
            params={"token": apify_token},
            timeout=15,
        )
        status_resp.raise_for_status()
        status = status_resp.json().get("data", {}).get("status", "")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            logging.error("Apify WTTJ run %s terminé avec statut : %s", run_id, status)
            return []

    items_resp = _req.get(
        f"{base}/datasets/{dataset_id}/items",
        params={"token": apify_token},
        timeout=30,
    )
    items_resp.raise_for_status()
    items = items_resp.json()

    offres = []
    for item in items:
        url = item.get("url") or item.get("applyUrl") or ""
        if not url:
            continue
        titre = item.get("name") or item.get("title") or ""
        entreprise = item.get("organizationName") or ""
        if not entreprise and isinstance(item.get("organization"), dict):
            entreprise = item["organization"].get("name", "")

        localisation = ""
        loc = item.get("location")
        if isinstance(loc, dict):
            localisation = loc.get("city") or loc.get("name") or ""
        elif isinstance(loc, str):
            localisation = loc
        if not localisation:
            offices = item.get("offices")
            if isinstance(offices, list) and offices:
                first = offices[0]
                if isinstance(first, dict):
                    localisation = first.get("city") or first.get("name") or ""

        sal_min = item.get("salaryMin")
        sal_max = item.get("salaryMax")
        sal_cur = item.get("salaryCurrency") or ""
        if sal_min and sal_max:
            salaire = f"{sal_min}-{sal_max} {sal_cur}".strip()
        elif sal_min:
            salaire = f"{sal_min} {sal_cur}".strip()
        else:
            salaire = ""

        offres.append({
            "titre": titre,
            "entreprise": entreprise,
            "localisation": localisation,
            "description": item.get("description", ""),
            "url": url,
            "source": "wttj",
            "date_publication": item.get("publishedAt", ""),
            "salaire": salaire,
        })

    logging.info("WTTJ — %d offres récupérées", len(offres))
    return offres


def _scraper_jobspy(
    search_term: str,
    location: str,
    results_wanted: int = 50,
    hours_old: int = 72,
    google_enabled: bool = True,
) -> list[dict]:
    """
    Scrape Indeed et Google Jobs via jobspy et retourne les offres au format unifié.
    results_wanted et hours_old sont lus depuis sources.yaml (section indeed).
    google_enabled permet d'activer/désactiver Google Jobs (défaut : activé).
    Retourne [] en cas d'erreur (package absent, réseau, etc.).
    """
    try:
        from jobspy import scrape_jobs
    except ImportError:
        logging.error("jobspy non installé — sources Indeed/Google ignorées (pip install python-jobspy)")
        return []

    site_name = ["indeed"] + (["google"] if google_enabled else [])

    df = scrape_jobs(
        site_name=site_name,
        search_term=search_term,
        google_search_term=f"{search_term} jobs {location}",
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        country_indeed="France",
    )

    if df is None or df.empty:
        return []

    offres = []
    for row in df.to_dict("records"):
        url = str(row.get("job_url") or row.get("url", ""))
        if not url:
            continue
        offres.append({
            "titre": str(row.get("title", "")),
            "entreprise": str(row.get("company", "")),
            "localisation": str(row.get("location", "")),
            "description": str(row.get("description") or row.get("job_description", "")),
            "url": url,
            "source": str(row.get("site") or "indeed"),
            "date_publication": str(row.get("date_posted", "")),
        })
    return offres


def _unifier_vers_db(offre: dict) -> dict:
    """Convertit le format unifié APEC/Indeed → format DB (compatible sauvegarder_offres)."""
    url = offre["url"]
    source = offre["source"]
    return {
        "id": f"{source}_{hashlib.md5(url.encode()).hexdigest()[:12]}",
        "intitule": offre["titre"],
        "description": offre["description"],
        "entreprise_nom": offre["entreprise"],
        "lieu_travail": offre["localisation"],
        "type_contrat": offre.get("type_contrat", ""),
        "salaire_libelle": offre.get("salaire", ""),
        "date_creation": offre["date_publication"],
        "url": url,
        "raw_json": json.dumps(offre, ensure_ascii=False),
        "collected_at": datetime.now().strftime("%Y-%m-%d"),
    }


def _collecter_emails_linkedin(gmail_address, app_password, expediteurs, label="INBOX", max_emails=20):
    if not gmail_address or not app_password:
        logging.warning("GMAIL_ADDRESS ou GMAIL_APP_PASSWORD manquant")
        return []
    offres_email: list[dict] = []
    urls_vues: set[str] = set()
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_address, app_password)
        mail.select(label)
        for expediteur in expediteurs:
            status, messages = mail.search(None, f'(UNSEEN FROM "{expediteur}")')
            if status != "OK":
                continue
            ids = messages[0].split()[-max_emails:]
            for email_id in ids:
                status, data = mail.fetch(email_id, "(RFC822)")
                if status != "OK":
                    continue
                msg = email.message_from_bytes(data[0][1])
                corps_html = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            corps_html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                            break
                else:
                    corps_html = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                soup = BeautifulSoup(corps_html, "html.parser")
                for job_card in soup.find_all("td", attrs={"data-test-id": "job-card"}):
                    lien = None
                    for a in job_card.find_all("a", href=True):
                        if "jobs/view/" in a["href"]:
                            lien = a
                            break
                    if not lien:
                        continue
                    url_propre = lien["href"].split("?")[0].rstrip("/")
                    if url_propre in urls_vues:
                        continue
                    urls_vues.add(url_propre)
                    titre_tag = job_card.find("a", class_=lambda c: c and "font-bold" in c)
                    titre = titre_tag.get_text(strip=True) if titre_tag else ""
                    entreprise = ""
                    localisation = ""
                    p_tags = job_card.find_all("p")
                    if p_tags:
                        entreprise_lieu = p_tags[0].get_text(strip=True)
                        if "·" in entreprise_lieu:
                            parties = entreprise_lieu.split("·", 1)
                            entreprise = parties[0].strip()
                            localisation = parties[1].strip()
                        else:
                            entreprise = entreprise_lieu.strip()
                    if titre and not titre.startswith("http"):
                        contenu = f"Titre du poste : {titre}\nEntreprise : {entreprise}\nLocalisation : {localisation}\nSource : LinkedIn\n"
                        offres_email.append({"url": url_propre, "contenu_email": contenu})
                        logging.info("LinkedIn extrait : %s | %s | %s", titre, entreprise, localisation)
                mail.store(email_id, "+FLAGS", "\\Seen")
        mail.logout()
        logging.info("Emails LinkedIn : %d offres extraites avec contenu", len(offres_email))
    except Exception as e:
        logging.error("Erreur IMAP Gmail : %s", e)
    return offres_email


def _importer_offre_depuis_url(url: str, db_path: str, profil_texte: str, contenu_email: str = "") -> bool:
    tavily_key = os.getenv("TAVILY_API_KEY", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not tavily_key or not anthropic_key:
        logging.warning("TAVILY_API_KEY ou ANTHROPIC_API_KEY manquant")
        return False
    try:
        contenu_fallback = contenu_email if contenu_email else url
        tavily_ok = False
        try:
            resp = httpx.post(
                "https://api.tavily.com/extract",
                json={"urls": [url], "api_key": tavily_key},
                timeout=30,
            )
            contenu = resp.json().get("results", [{}])[0].get("raw_content", "")
            tavily_ok = bool(contenu)
        except Exception as tavily_exc:
            logging.warning("Tavily Extract échoué pour %s : %s", url, tavily_exc)
            contenu = ""
        contenu = contenu if contenu else contenu_fallback
        if not contenu:
            logging.warning("Aucun contenu disponible pour %s — offre ignorée", url)
            return False
        logging.debug("Contenu envoyé à Haiku pour %s : %s", url, contenu[:500])
        if "linkedin" in url:
            logging.info("Contenu LinkedIn envoyé à Haiku : %s", contenu[:200])
        client = anthropic.Anthropic(api_key=anthropic_key)
        if tavily_ok:
            prompt = f"""Extrais les informations de cette offre d'emploi.
Retourne UNIQUEMENT ce JSON, sans aucun texte avant ou après :
{{"titre": "...", "entreprise": "...", "localisation": "...", "type_contrat": "", "salaire": "", "description": "..."}}

Remplace les "..." par les valeurs trouvées. Si absent, mets "".

Offre :
{contenu[:3000]}
"""
        else:
            prompt = f"""Extrais les informations de cette offre d'emploi LinkedIn.
Le contenu est partiel (extrait d'un email d'alerte).
Retourne UNIQUEMENT ce JSON :
{{"titre": "...", "entreprise": "...", "localisation": "...", "type_contrat": "", "salaire": "", "description": "Offre LinkedIn - description complète sur le lien"}}

Remplace les "..." par les valeurs trouvées. Pour type_contrat et salaire mets "" si absent.
Ne retourne rien d'autre que le JSON.

Contenu disponible :
{contenu[:2000]}
"""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        texte = response.content[0].text.strip()
        texte = texte.replace("```json", "").replace("```", "").strip()
        if "{" in texte and "}" in texte:
            texte = texte[texte.index("{"):texte.rindex("}")+1]
            offre_dict = json.loads(texte)
        else:
            logging.warning("Haiku n'a pas retourné de JSON pour %s — construction minimale", url)
            offre_dict = {
                "titre": contenu[:100] if contenu else "",
                "entreprise": "",
                "localisation": "",
                "type_contrat": "",
                "salaire": "",
                "description": contenu[:500] if contenu else "",
            }
        offre_dict["url"] = url
        if not offre_dict.get("titre", "").strip():
            logging.warning("Offre ignorée — titre vide pour %s", url)
            return False
        offre_db = {
            "id": f"linkedin_{hashlib.md5(url.encode()).hexdigest()[:12]}",
            "intitule": offre_dict.get("titre", ""),
            "description": offre_dict.get("description", ""),
            "entreprise_nom": offre_dict.get("entreprise", ""),
            "lieu_travail": offre_dict.get("localisation", ""),
            "type_contrat": offre_dict.get("type_contrat", ""),
            "salaire_libelle": offre_dict.get("salaire", ""),
            "date_creation": datetime.now().strftime("%Y-%m-%d"),
            "url": url,
            "raw_json": json.dumps(offre_dict, ensure_ascii=False),
            "source": "linkedin",
            "collected_at": datetime.now().strftime("%Y-%m-%d"),
        }
        with get_connection(db_path) as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO offres
                (id, intitule, description, entreprise_nom, lieu_travail,
                 type_contrat, salaire_libelle, date_creation, url, raw_json, source,
                 collected_at)
                VALUES
                (:id, :intitule, :description, :entreprise_nom, :lieu_travail,
                 :type_contrat, :salaire_libelle, :date_creation, :url, :raw_json, :source,
                 :collected_at)
            """, offre_db)
            conn.commit()
            if cursor.rowcount == 0:
                logging.info("Offre déjà en DB : %s", url)
                return False
        from scorer import scorer_offre, mettre_a_jour_score
        with get_connection(db_path) as conn:
            offre_row = conn.execute("SELECT * FROM offres WHERE id = ?", (offre_db["id"],)).fetchone()
        if offre_row:
            score, explication, points_forts, points_faibles = scorer_offre(offre_row, profil_texte)
            mettre_a_jour_score(offre_db["id"], score, explication, points_forts, points_faibles, db_path)
            logging.info("LinkedIn importé : %s — %d/100", offre_dict.get("titre", ""), score)
        return True
    except Exception as e:
        logging.error("Erreur import URL %s : %s", url, e)
        return False


# ─────────────────────────────────────────────
# Noeud 0 : Collecte emails LinkedIn (Gmail IMAP)
# ─────────────────────────────────────────────

def collecter_emails(state: AgentState) -> AgentState:
    console.print("\n[bold cyan]▶ Collecte emails LinkedIn — Gmail IMAP[/bold cyan]")
    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    app_password = os.getenv("GMAIL_APP_PASSWORD", "")
    db_path = os.getenv("DB_PATH", "data/offers.db")
    profil_path = os.getenv("PROFILE_PATH", "config/profile.yaml")
    _, sources = charger_config()
    gmail_cfg = sources.get("gmail_imap", {})
    if not gmail_cfg.get("enabled", False):
        console.print("[yellow]  Gmail IMAP désactivé[/yellow]")
        return state
    with open(profil_path, encoding="utf-8") as f:
        profil = yaml.safe_load(f)
    from scorer import formater_profil
    profil_texte = formater_profil(profil)
    expediteurs = gmail_cfg.get("expediteurs", ["jobs-noreply@linkedin.com"])
    label = gmail_cfg.get("label", "INBOX")
    max_emails = gmail_cfg.get("max_emails", 20)
    offres_email = _collecter_emails_linkedin(gmail_address, app_password, expediteurs, label, max_emails)
    if not offres_email:
        console.print("  [dim]Aucune nouvelle alerte LinkedIn[/dim]")
        return state
    console.print(f"  [cyan]{len(offres_email)} offres LinkedIn trouvées[/cyan]")
    importees = 0
    for offre_email in offres_email:
        if _importer_offre_depuis_url(offre_email["url"], db_path, profil_texte, contenu_email=offre_email["contenu_email"]):
            importees += 1
        time.sleep(2)
    console.print(f"[green]✓[/green] {importees} offres LinkedIn importées")

    # Rescorer les offres LinkedIn avec score 0 ou NULL (importées sans contenu suffisant)
    from scorer import scorer_offre, mettre_a_jour_score
    with get_connection(db_path) as conn:
        offres_a_rescorer = conn.execute("""
            SELECT * FROM offres
            WHERE source = 'linkedin'
            AND (score = 0 OR score IS NULL)
            ORDER BY collected_at DESC
        """).fetchall()

    if offres_a_rescorer:
        console.print(f"  [cyan]Rescoring de {len(offres_a_rescorer)} offres LinkedIn avec score 0[/cyan]")
        rescored = 0
        for offre_row in offres_a_rescorer:
            offre_dict = dict(offre_row)
            if not offre_dict.get("intitule") or offre_dict["intitule"].startswith("http"):
                continue
            score, explication, points_forts, points_faibles = scorer_offre(offre_row, profil_texte)
            mettre_a_jour_score(offre_dict["id"], score, explication, points_forts, points_faibles, db_path)
            rescored += 1
            time.sleep(1)
        console.print(f"[green]✓[/green] {rescored} offres LinkedIn rescorées")

    return {**state, "new_offers_count": state.get("new_offers_count", 0) + importees}


# ─────────────────────────────────────────────
# Noeud 1 : Collecte des offres
# ─────────────────────────────────────────────

def collecter(state: AgentState) -> AgentState:
    """Collecte en parallèle depuis Adzuna, APEC, WTTJ et Indeed/Google, puis sauvegarde les nouvelles offres."""
    console.print("\n[bold cyan]▶ Étape 1 / 4 — Collecte des offres[/bold cyan]")

    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    apify_token = os.getenv("APIFY_API_TOKEN", "")
    db_path = os.getenv("DB_PATH", "data/offers.db")

    profil, sources = charger_config()
    search_term = profil["candidat"]["poste_cible"]
    deal_breakers = profil.get("deal_breakers", [])

    # Paramètres APEC depuis sources.yaml
    apec_cfg = sources.get("apec", {})

    # Paramètres Indeed/Google depuis sources.yaml
    indeed_cfg = sources.get("indeed", {})
    indeed_results_wanted = indeed_cfg.get("results_wanted", 50)
    indeed_hours_old = indeed_cfg.get("hours_old", 72)
    indeed_location = indeed_cfg.get("location", "France")

    # Paramètres WTTJ depuis sources.yaml
    wttj_cfg = sources.get("wttj", {})

    # Paramètres Google Jobs depuis sources.yaml
    google_cfg = sources.get("google_jobs", {})
    google_enabled = google_cfg.get("enabled", True)

    init_db(db_path)

    # Lancer les quatre scrapers en parallèle
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_scraper_adzuna, app_id, app_key, profil, sources, db_path): "adzuna",
            executor.submit(_scraper_apec, apec_cfg, apify_token): "apec",
            executor.submit(_scraper_wttj, wttj_cfg, apify_token, search_term): "wttj",
            executor.submit(
                _scraper_jobspy, search_term, indeed_location,
                indeed_results_wanted, indeed_hours_old, google_enabled,
            ): "jobspy",
        }
        offres_adzuna_nouvelles = 0
        offres_externes: list[dict] = []

        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
                if source == "adzuna":
                    offres_adzuna_nouvelles = result
                    console.print(f"  [dim]ADZUNA[/dim] — [cyan]{result} nouvelles[/cyan]")
                else:
                    console.print(
                        f"  [dim]{source.upper()}[/dim] — {len(result)} offres récupérées"
                    )
                    offres_externes.extend(result)
            except Exception as exc:
                logging.error("Erreur source %s : %s", source, exc)
                console.print(f"  [red]Erreur {source.upper()} :[/red] {exc}")

    # Déduplication par URL (APEC + Indeed)
    seen_urls: set[str] = set()
    offres_dedupliquees: list[dict] = []
    for o in offres_externes:
        url = o.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            offres_dedupliquees.append(o)

    # Filtrage deal-breakers + normalisation DB
    offres_db = []
    for o in offres_dedupliquees:
        texte = f"{o.get('titre', '')} {o.get('description', '')}".lower()
        if not any(db_kw.lower() in texte for db_kw in deal_breakers):
            offres_db.append(_unifier_vers_db(o))

    # Pré-filtre anti-doublons URL (inclut les offres Adzuna déjà insérées)
    offres_db = _filtrer_urls_existantes(offres_db, db_path)
    nouvelles_externes = sauvegarder_offres(offres_db, db_path)
    total_nouvelles = offres_adzuna_nouvelles + nouvelles_externes
    console.print(f"[green]✓[/green] {total_nouvelles} nouvelles offres collectées au total")
    return {**state, "new_offers_count": total_nouvelles}


# ─────────────────────────────────────────────
# Edge conditionnel
#
# C'est ici que l'agent "décide" : si aucune nouvelle offre,
# inutile de scorer — on saute directement au rapport.
# Cette logique conditionnelle est le cœur de l'agentique.
# ─────────────────────────────────────────────

def should_score(state: AgentState) -> str:
    if state["new_offers_count"] > 0:
        console.print("[dim]→ Nouvelles offres détectées, passage au scoring[/dim]")
        return "scorer_batch"
    console.print("[yellow]→ Aucune nouvelle offre, scoring ignoré[/yellow]")
    return "generer_rapport"


# ─────────────────────────────────────────────
# Noeud 2 : Scoring des offres
# ─────────────────────────────────────────────

def scorer_batch(state: AgentState) -> AgentState:
    """Score toutes les offres non scorées avec Claude Haiku."""
    console.print("\n[bold cyan]▶ Étape 2 / 3 — Scoring des offres[/bold cyan]")

    db_path = os.getenv("DB_PATH", "data/offers.db")
    profil_path = os.getenv("PROFILE_PATH", "config/profile.yaml")

    with open(profil_path, encoding="utf-8") as f:
        profil = yaml.safe_load(f)

    profil_texte = formater_profil(profil)

    with get_connection(db_path) as conn:
        offres = conn.execute(
            "SELECT * FROM offres WHERE score IS NULL ORDER BY collected_at DESC"
        ).fetchall()

    if not offres:
        console.print("[yellow]Aucune offre à scorer.[/yellow]")
        return {**state, "scored_count": 0}

    console.print(f"[cyan]{len(offres)} offres à scorer...[/cyan]")
    scores_ok = 0

    for offre in offres:
        offre_dict = dict(offre)
        intitule = (offre_dict.get("intitule") or "")[:50]
        console.print(f"  [dim]Scoring :[/dim] {intitule}")

        score, explication, points_forts, points_faibles = scorer_offre(
            offre, profil_texte
        )
        mettre_a_jour_score(
            offre_dict["id"], score, explication, points_forts, points_faibles, db_path
        )
        if score >= 0:
            scores_ok += 1
        time.sleep(1)

    console.print(f"[green]✓[/green] {scores_ok} offres scorées")
    return {**state, "scored_count": scores_ok}


# ─────────────────────────────────────────────
# Noeud 3 : Génération du rapport
# ─────────────────────────────────────────────

def generer_rapport(state: AgentState) -> AgentState:
    """Exporte toutes les offres scorées dans un fichier texte."""
    console.print("\n[bold cyan]▶ Étape 3 / 3 — Génération du rapport[/bold cyan]")

    db_path = os.getenv("DB_PATH", "data/offers.db")

    with get_connection(db_path) as conn:
        offres = conn.execute(
            "SELECT * FROM offres WHERE score IS NOT NULL AND score >= 0 ORDER BY score DESC"
        ).fetchall()

    offres = [dict(o) for o in offres]

    if not offres:
        console.print("[yellow]Aucune offre scorée à exporter.[/yellow]")
        return {**state, "report_path": None}

    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    chemin = f"data/offres_{horodatage}.txt"
    exporter_txt(offres, chemin)

    console.print(f"[green]✓[/green] Rapport généré : [bold]{os.path.abspath(chemin)}[/bold]")
    return {**state, "report_path": chemin}


# ─────────────────────────────────────────────
# Noeud 4 : Génération du fichier Excel
# ─────────────────────────────────────────────

def generer_excel(state: AgentState) -> AgentState:
    """Exporte toutes les offres scorées dans data/offres.xlsx (fichier permanent, écrasé à chaque run)."""
    console.print("\n[bold cyan]▶ Étape 4 / 4 — Génération du fichier Excel[/bold cyan]")

    db_path = os.getenv("DB_PATH", "data/offers.db")

    with get_connection(db_path) as conn:
        offres = conn.execute(
            "SELECT * FROM offres WHERE score IS NOT NULL AND score >= 0 ORDER BY score DESC"
        ).fetchall()

    offres = [dict(o) for o in offres]

    if not offres:
        console.print("[yellow]Aucune offre scorée pour le fichier Excel.[/yellow]")
        return {**state, "excel_path": None}

    chemin = "data/offres.xlsx"

    # Couleurs de remplissage
    VERT  = PatternFill("solid", fgColor="C6EFCE")   # score >= 85
    JAUNE = PatternFill("solid", fgColor="FFEB9C")   # score >= 60
    GRIS  = PatternFill("solid", fgColor="F2F2F2")   # sinon

    colonnes = [
        "Score", "Priorité", "Poste", "Entreprise", "Lieu",
        "Contrat", "Salaire", "Source", "Analyse", "Points forts",
        "Points faibles", "URL",
    ]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Offres"

    # Ligne d'en-tête
    ws.append(colonnes)
    header_font = Font(bold=True)
    for cell in ws[1]:
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # Largeurs de colonnes
    largeurs = [8, 20, 35, 25, 20, 15, 20, 12, 60, 40, 40, 50]
    for i, larg in enumerate(largeurs, 1):
        ws.column_dimensions[get_column_letter(i)].width = larg

    # Remplissage des lignes de données
    for offre in offres:
        score = offre.get("score") or 0

        if score >= 85:
            priorite = "★★★ PRIORITAIRE"
            fill = VERT
        elif score >= 60:
            priorite = "★★☆ À CONSIDÉRER"
            fill = JAUNE
        else:
            priorite = "★☆☆ FAIBLE"
            fill = GRIS

        # Déduire la source depuis l'URL ou la colonne source
        url = offre.get("url") or ""
        if "adzuna" in url:
            source = "Adzuna"
        elif "apec" in url:
            source = "APEC"
        elif "indeed" in url:
            source = "Indeed"
        elif "welcometothejungle" in url:
            source = "WTTJ"
        elif offre.get("source") == "google":
            source = "Google"
        else:
            source = "—"

        ligne = [
            score,
            priorite,
            offre.get("intitule") or "",
            offre.get("entreprise_nom") or "",
            offre.get("lieu_travail") or "",
            offre.get("type_contrat") or "",
            offre.get("salaire_libelle") or "",
            source,
            offre.get("score_explication") or "",
            offre.get("score_points_forts") or "",
            offre.get("score_points_faibles") or "",
            url,
        ]
        ws.append(ligne)

        row_idx = ws.max_row
        for cell in ws[row_idx]:
            cell.fill = fill
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    # Filtres automatiques sur toutes les colonnes
    ws.auto_filter.ref = ws.dimensions

    # Gel de la première ligne
    ws.freeze_panes = "A2"

    wb.save(chemin)
    console.print(f"[green]✓[/green] Fichier Excel généré : [bold]{os.path.abspath(chemin)}[/bold]")
    return {**state, "excel_path": chemin}


# ─────────────────────────────────────────────
# Construction du graphe LangGraph
# ─────────────────────────────────────────────

def construire_graphe():
    """
    Construit et compile le graphe agentique.

    Différence clé avec les scripts manuels :
    - Avant : vous lanciez collector.py, scorer.py, dashboard.py à la main
    - Maintenant : le graphe décide de l'ordre et des conditions d'exécution
    """
    builder = StateGraph(AgentState)

    builder.add_node("collecter_emails", collecter_emails)
    builder.add_node("collecter", collecter)
    builder.add_node("scorer_batch", scorer_batch)
    builder.add_node("generer_rapport", generer_rapport)
    builder.add_node("generer_excel", generer_excel)

    builder.add_edge(START, "collecter_emails")
    builder.add_edge("collecter_emails", "collecter")
    builder.add_conditional_edges(
        "collecter",
        should_score,
        {
            "scorer_batch": "scorer_batch",
            "generer_rapport": "generer_rapport",
        },
    )
    builder.add_edge("scorer_batch", "generer_rapport")
    builder.add_edge("generer_rapport", "generer_excel")
    builder.add_edge("generer_excel", END)

    return builder.compile()


# ─────────────────────────────────────────────
# Point d'entrée principal
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pipeline agentique LangGraph — collect → score → rapport"
    )
    parser.add_argument(
        "--visualiser",
        action="store_true",
        help="Génère et ouvre un schéma PNG du graphe agentique (sans exécuter le pipeline)",
    )
    args = parser.parse_args()

    app = construire_graphe()

    if args.visualiser:
        console.print("\n[bold cyan]Génération du schéma du graphe agentique...[/bold cyan]")
        try:
            png = app.get_graph().draw_mermaid_png()
            chemin_png = "pipeline_graph.png"
            with open(chemin_png, "wb") as f:
                f.write(png)
            console.print(f"[green]✓[/green] Schéma généré : [bold]{os.path.abspath(chemin_png)}[/bold]")
            os.startfile(chemin_png)
        except Exception as e:
            console.print(f"[yellow]Impossible de générer le PNG :[/yellow] {e}")
            console.print("[dim]Schéma Mermaid (à coller sur mermaid.live) :[/dim]\n")
            console.print(app.get_graph().draw_mermaid())
        return

    # Exécution du pipeline complet
    console.print("\n[bold cyan]Agent de Recherche d'Emploi — Pipeline LangGraph[/bold cyan]")
    console.print("━" * 50)

    if not os.getenv("ANTHROPIC_API_KEY"):
        console.print("[red]Erreur :[/red] ANTHROPIC_API_KEY manquante dans .env")
        raise SystemExit(1)

    if not os.getenv("ADZUNA_APP_ID") or not os.getenv("ADZUNA_APP_KEY"):
        console.print("[red]Erreur :[/red] ADZUNA_APP_ID et ADZUNA_APP_KEY manquants dans .env")
        raise SystemExit(1)

    etat_initial: AgentState = {
        "new_offers_count": 0,
        "scored_count": 0,
        "report_path": None,
        "excel_path": None,
    }

    etat_final = app.invoke(etat_initial)

    console.print()
    console.print(Panel(
        f"Nouvelles offres collectées : [cyan]{etat_final['new_offers_count']}[/cyan]\n"
        f"Offres scorées : [green]{etat_final['scored_count']}[/green]\n"
        f"Rapport texte  : [bold]{etat_final['report_path'] or 'Non généré'}[/bold]\n"
        f"📊 Excel        : [bold]{os.path.abspath(etat_final['excel_path']) if etat_final.get('excel_path') else 'Non généré'}[/bold]",
        title="[bold]Résumé du pipeline[/bold]",
        border_style="green",
    ))


if __name__ == "__main__":
    main()
