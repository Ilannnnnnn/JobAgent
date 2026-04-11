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

import yaml
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
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
    profile: dict
    new_offers_count: int
    scored_count: int
    report_path: Optional[str]


# ─────────────────────────────────────────────
# Helpers de scraping (sources externes)
# ─────────────────────────────────────────────

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
        total += sauvegarder_offres(offres_normalisees, db_path)
        time.sleep(1)

    return total


def _scraper_apec(search_term: str, location: str, apify_token: str) -> list[dict]:
    """
    Lance l'actor Apify APEC et retourne les offres au format unifié.
    Retourne [] en cas d'erreur (token manquant, timeout, etc.).
    """
    import requests as _req

    if not apify_token:
        logging.warning("APIFY_API_TOKEN manquant — source APEC ignorée")
        return []

    actor_id = os.getenv("APIFY_ACTOR_APEC", "easyapi~apec-jobs-scraper")
    base = "https://api.apify.com/v2"
    headers = {"Content-Type": "application/json"}

    # Lancer le run
    run_resp = _req.post(
        f"{base}/acts/{actor_id}/runs",
        params={"token": apify_token},
        json={"keywords": search_term, "location": location},
        headers=headers,
        timeout=30,
    )
    run_resp.raise_for_status()
    run_data = run_resp.json().get("data", {})
    run_id = run_data.get("id")
    dataset_id = run_data.get("defaultDatasetId")

    # Polling jusqu'à SUCCEEDED (max 120 s)
    for _ in range(24):
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

    offres = []
    for item in items:
        url = item.get("url") or item.get("applyUrl", "")
        if not url:
            continue
        offres.append({
            "titre": item.get("title") or item.get("titre", ""),
            "entreprise": item.get("company") or item.get("entreprise", ""),
            "localisation": item.get("location") or item.get("localisation", ""),
            "description": item.get("description", ""),
            "url": url,
            "source": "apec",
            "date_publication": item.get("date") or item.get("datePublication", ""),
        })
    return offres


def _scraper_indeed(search_term: str, location: str) -> list[dict]:
    """
    Scrape Indeed via jobspy et retourne les offres au format unifié.
    Retourne [] en cas d'erreur (package absent, réseau, etc.).
    """
    try:
        from jobspy import scrape_jobs
    except ImportError:
        logging.error("jobspy non installé — source Indeed ignorée (pip install python-jobspy)")
        return []

    df = scrape_jobs(
        site_name=["indeed"],
        search_term=search_term,
        location=location,
        results_wanted=20,
        hours_old=72,
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
            "source": "indeed",
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
        "type_contrat": "",
        "salaire_libelle": "",
        "date_creation": offre["date_publication"],
        "url": url,
        "raw_json": json.dumps(offre, ensure_ascii=False),
    }


# ─────────────────────────────────────────────
# Noeud 1 : Collecte des offres
# ─────────────────────────────────────────────

def collecter(state: AgentState) -> AgentState:
    """Collecte en parallèle depuis Adzuna, APEC et Indeed, puis sauvegarde les nouvelles offres."""
    console.print("\n[bold cyan]▶ Étape 1 / 3 — Collecte des offres[/bold cyan]")

    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    apify_token = os.getenv("APIFY_API_TOKEN", "")
    db_path = os.getenv("DB_PATH", "data/offers.db")

    profil, sources = charger_config()
    search_term = profil["candidat"]["poste_cible"]
    deal_breakers = profil.get("deal_breakers", [])

    init_db(db_path)

    # Lancer les trois scrapers en parallèle
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_scraper_adzuna, app_id, app_key, profil, sources, db_path): "adzuna",
            executor.submit(_scraper_apec, search_term, "France", apify_token): "apec",
            executor.submit(_scraper_indeed, search_term, "France"): "indeed",
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

    # Filtrage deal-breakers + normalisation DB + sauvegarde
    offres_db = []
    for o in offres_dedupliquees:
        texte = f"{o.get('titre', '')} {o.get('description', '')}".lower()
        if not any(db_kw.lower() in texte for db_kw in deal_breakers):
            offres_db.append(_unifier_vers_db(o))

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
    """Score toutes les offres non scorées avec Gemini via LangChain."""
    console.print("\n[bold cyan]▶ Étape 2 / 3 — Scoring des offres[/bold cyan]")

    api_key = os.getenv("GOOGLE_AI_STUDIO_KEY")
    db_path = os.getenv("DB_PATH", "data/offers.db")
    profil_path = os.getenv("PROFILE_PATH", "config/profile.yaml")

    with open(profil_path, encoding="utf-8") as f:
        profil = yaml.safe_load(f)

    profil_texte = formater_profil(profil)

    llm = ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite-preview",
        google_api_key=api_key,
        temperature=0.2,
        max_output_tokens=1024,
    )

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
            offre, profil_texte, llm
        )
        mettre_a_jour_score(
            offre_dict["id"], score, explication, points_forts, points_faibles, db_path
        )
        if score >= 0:
            scores_ok += 1
        time.sleep(4)

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

    builder.add_node("collecter", collecter)
    builder.add_node("scorer_batch", scorer_batch)
    builder.add_node("generer_rapport", generer_rapport)

    builder.add_edge(START, "collecter")
    builder.add_conditional_edges(
        "collecter",
        should_score,
        {
            "scorer_batch": "scorer_batch",
            "generer_rapport": "generer_rapport",
        },
    )
    builder.add_edge("scorer_batch", "generer_rapport")
    builder.add_edge("generer_rapport", END)

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

    if not os.getenv("GOOGLE_AI_STUDIO_KEY"):
        console.print("[red]Erreur :[/red] GOOGLE_AI_STUDIO_KEY manquante dans .env")
        raise SystemExit(1)

    if not os.getenv("ADZUNA_APP_ID") or not os.getenv("ADZUNA_APP_KEY"):
        console.print("[red]Erreur :[/red] ADZUNA_APP_ID et ADZUNA_APP_KEY manquants dans .env")
        raise SystemExit(1)

    etat_initial: AgentState = {
        "profile": {},
        "new_offers_count": 0,
        "scored_count": 0,
        "report_path": None,
    }

    etat_final = app.invoke(etat_initial)

    console.print()
    console.print(Panel(
        f"Nouvelles offres collectées : [cyan]{etat_final['new_offers_count']}[/cyan]\n"
        f"Offres scorées : [green]{etat_final['scored_count']}[/green]\n"
        f"Rapport : [bold]{etat_final['report_path'] or 'Non généré'}[/bold]",
        title="[bold]Résumé du pipeline[/bold]",
        border_style="green",
    ))


if __name__ == "__main__":
    main()
