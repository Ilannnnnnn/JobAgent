"""
Dashboard Streamlit — Consultation et suivi des offres d'emploi.

Utilisation :
    streamlit run src/dashboard.py

Conserve la fonction exporter_txt() utilisée par pipeline.py.
"""

import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime

import anthropic
import folium
import httpx
import pandas as pd
import streamlit as st
import yaml
from dotenv import load_dotenv
from streamlit_folium import st_folium

# Ajouter src/ au path pour importer db.py
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection
from scorer import scorer_offre, mettre_a_jour_score, formater_profil

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "data/offers.db")


# ─────────────────────────────────────────────
# Fonction conservée pour pipeline.py
# ─────────────────────────────────────────────

def exporter_txt(offres: list, chemin: str) -> None:
    """
    Génère un fichier texte lisible avec toutes les infos des offres scorées.
    Une section par offre, avec score, analyse, points forts/faibles et lien.
    """
    maintenant = datetime.now().strftime("%d/%m/%Y à %H:%M")
    lignes = [
        "=" * 70,
        f"  RAPPORT DE RECHERCHE D'EMPLOI — {maintenant}",
        f"  {len(offres)} offres scorées",
        "=" * 70,
        "",
    ]

    for rang, offre in enumerate(offres, 1):
        score = offre["score"]

        if score >= 85:
            indicateur = "★★★  PRIORITAIRE"
        elif score >= 60:
            indicateur = "★★☆  À CONSIDÉRER"
        else:
            indicateur = "★☆☆  FAIBLE"

        lignes += [
            f"{'─' * 70}",
            f"  #{rang}  [{score}/100] {indicateur}",
            f"{'─' * 70}",
            f"  ID         : {offre['id']}",
            f"  Poste      : {offre['intitule'] or 'N/A'}",
            f"  Entreprise : {offre['entreprise_nom'] or 'N/A'}",
            f"  Lieu       : {offre['lieu_travail'] or 'N/A'}",
            f"  Contrat    : {offre['type_contrat'] or 'N/A'}",
            f"  Salaire    : {offre['salaire_libelle'] or 'Non précisé'}",
            f"  Lien       : {offre['url'] or 'N/A'}",
            "",
        ]

        if offre.get("score_explication"):
            lignes += [
                "  ANALYSE :",
                f"  {offre['score_explication']}",
                "",
            ]

        try:
            points_forts = json.loads(offre.get("score_points_forts") or "[]")
            if points_forts:
                lignes.append("  POINTS FORTS :")
                for p in points_forts:
                    lignes.append(f"    + {p}")
                lignes.append("")
        except (json.JSONDecodeError, TypeError):
            pass

        try:
            points_faibles = json.loads(offre.get("score_points_faibles") or "[]")
            if points_faibles:
                lignes.append("  POINTS FAIBLES :")
                for p in points_faibles:
                    lignes.append(f"    - {p}")
                lignes.append("")
        except (json.JSONDecodeError, TypeError):
            pass

        lignes.append("")

    lignes += [
        "=" * 70,
        "  FIN DU RAPPORT",
        "=" * 70,
    ]

    with open(chemin, "w", encoding="utf-8") as f:
        f.write("\n".join(lignes))


# ─────────────────────────────────────────────
# Helpers Streamlit
# ─────────────────────────────────────────────

def deriver_source(url: str) -> str:
    url = (url or "").lower()
    if "adzuna" in url:             return "Adzuna"
    if "apec" in url:               return "Apec"
    if "indeed" in url:             return "Indeed"
    if "welcometothejungle" in url: return "Wttj"
    if "google" in url:             return "Google"
    if "linkedin.com/comm" in url or "linkedin.com/jobs" in url: return "Linkedin"
    return "N/A"


def mettre_a_jour_statut(offre_id: str, nouveau_statut: str, db_path: str) -> None:
    with get_connection(db_path) as conn:
        if nouveau_statut == "Postulé":
            conn.execute(
                "UPDATE offres SET statut = ?, date_postulation = ? WHERE id = ?",
                (nouveau_statut, datetime.now().strftime("%d/%m/%Y"), offre_id)
            )
        else:
            conn.execute(
                "UPDATE offres SET statut = ? WHERE id = ?",
                (nouveau_statut, offre_id)
            )
        conn.commit()



def charger_offres(db_path: str) -> pd.DataFrame:
    """Charge toutes les offres scorées depuis la DB."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM offres WHERE score >= 0 OR score IS NULL ORDER BY COALESCE(score, 0) DESC"
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def colorier_texte(row):
    score = row["Score"]
    if score >= 85:
        color = "color: #1a6b3a"
    elif score >= 60:
        color = "color: #7a4f00"
    else:
        color = "color: #666666"
    return [color] * len(row)


def rendre_badges_html(score: int, priorite: str, source: str) -> str:
    if score >= 85:
        s_bg, s_fg = "#d4edda", "#155724"
    elif score >= 60:
        s_bg, s_fg = "#fff3cd", "#856404"
    else:
        s_bg, s_fg = "#f8d7da", "#721c24"

    label_p = priorite.replace("★★★ ", "").replace("★★ ", "").replace("★ ", "")
    if "PRIORITAIRE" in label_p:
        p_bg, p_fg = "#d4edda", "#155724"
    elif "CONSIDÉRER" in label_p:
        p_bg, p_fg = "#fff3cd", "#856404"
    else:
        p_bg, p_fg = "#e2e3e5", "#383d41"

    style = (
        "display:inline-block;padding:2px 10px;border-radius:12px;"
        "font-size:0.85em;font-weight:600;margin-right:6px;"
    )
    return (
        f'<span style="{style}background:{s_bg};color:{s_fg};">Score {score}/100</span>'
        f'<span style="{style}background:{p_bg};color:{p_fg};">{label_p}</span>'
        f'<span style="{style}background:#cfe2ff;color:#084298;">{source}</span>'
    )


def construire_graphique_salaire(salaire_actuel, tous_salaires):
    def extraire(lib):
        if not lib:
            return []
        vals = []
        for m in re.findall(r'\d[\d\s]*', lib):
            try:
                v = int(m.replace(" ", ""))
                if v < 500:
                    v *= 12
                vals.append(v)
            except ValueError:
                pass
        return vals

    population = []
    for lib in tous_salaires:
        population.extend(extraire(lib))
    if len(population) < 2:
        return None

    marche_min = min(population) / 1000
    marche_max = max(population) / 1000
    mediane    = statistics.median(population) / 1000
    etendue    = marche_max - marche_min or 1  # évite division par zéro

    vals_offre = extraire(salaire_actuel)
    offre_min  = min(vals_offre) / 1000 if vals_offre else None
    offre_max  = max(vals_offre) / 1000 if vals_offre else None

    pct_mediane = (mediane - marche_min) / etendue * 100
    pct_offre   = (offre_min - marche_min) / etendue * 100 if offre_min is not None else None

    label_offre = (
        f"{offre_min:.0f}–{offre_max:.0f}k€" if offre_min != offre_max and offre_max is not None
        else f"{offre_min:.0f}k€"
    ) if offre_min is not None else None

    trait_offre = (
        f'<div style="position:absolute;left:{pct_offre:.1f}%;top:0;bottom:0;'
        f'width:3px;background:#22c55e;border-radius:2px;"></div>'
    ) if pct_offre is not None else ""

    centre_label = (
        f'<span style="color:#22c55e;font-weight:700;">Cette offre : {label_offre}</span>'
        if label_offre else '<span style="color:#6b7280;">—</span>'
    )

    return f"""
<div style="background:#1e2530;border-radius:8px;padding:16px 18px;font-family:sans-serif;">
  <div style="font-size:0.7em;letter-spacing:.08em;color:#6b7280;margin-bottom:10px;">SALAIRE VS MARCHÉ</div>
  <div style="position:relative;height:12px;background:#374151;border-radius:6px;overflow:visible;margin-bottom:8px;">
    <div style="position:absolute;left:0;top:0;bottom:0;width:{pct_mediane:.1f}%;background:#3b82f6;border-radius:6px 0 0 6px;"></div>
    {trait_offre}
  </div>
  <div style="display:flex;justify-content:space-between;font-size:0.78em;margin-bottom:6px;">
    <span style="color:#6b7280;">{marche_min:.0f}k€</span>
    {centre_label}
    <span style="color:#6b7280;">{marche_max:.0f}k€</span>
  </div>
  <div style="text-align:center;font-size:0.75em;color:#6b7280;">Médiane marché : ~{mediane:.0f}k€</div>
</div>"""


def importer_offre_manuelle(input_offre: str, mode: str, db_path: str) -> tuple[dict, int]:
    """
    Importe une offre depuis une URL (via Tavily Extract) ou du texte brut,
    extrait les champs via Claude Haiku, insère en DB et score immédiatement.
    Retourne (offre_dict, score).
    """
    if mode == "URL":
        tavily_key = os.getenv("TAVILY_API_KEY", "")
        resp = httpx.post(
            "https://api.tavily.com/extract",
            json={"urls": [input_offre], "api_key": tavily_key},
            timeout=30,
        )
        resp.raise_for_status()
        contenu = resp.json().get("results", [{}])[0].get("raw_content", "")
    else:
        contenu = input_offre

    client_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = f"""Extrais les informations de cette offre d'emploi et retourne UNIQUEMENT un JSON valide avec ces champs exacts :
{{
  "titre": "...",
  "entreprise": "...",
  "localisation": "...",
  "type_contrat": "...",
  "salaire": "...",
  "description": "...",
  "url": "..."
}}

Si un champ est absent, mets une chaîne vide "".
Pour l'URL : si le mode est URL utilise l'URL fournie, sinon mets "".
Ne retourne rien d'autre que le JSON.

Offre :
{contenu[:6000]}
"""

    response = client_ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    texte = response.content[0].text.strip()
    texte = texte.strip("```json").strip("```").strip()
    offre_dict = json.loads(texte)

    if mode == "URL" and not offre_dict.get("url"):
        offre_dict["url"] = input_offre

    url = offre_dict.get("url", "")
    offre_db = {
        "id": f"manuel_{hashlib.md5(url.encode() if url else os.urandom(16)).hexdigest()[:12]}",
        "intitule": offre_dict.get("titre", ""),
        "description": offre_dict.get("description", ""),
        "entreprise_nom": offre_dict.get("entreprise", ""),
        "lieu_travail": offre_dict.get("localisation", ""),
        "type_contrat": offre_dict.get("type_contrat", ""),
        "salaire_libelle": offre_dict.get("salaire", ""),
        "date_creation": datetime.now().strftime("%Y-%m-%d"),
        "url": url,
        "raw_json": json.dumps(offre_dict, ensure_ascii=False),
        "source": "manuel",
        "collected_at": datetime.now().strftime("%Y-%m-%d"),
    }

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO offres
            (id, intitule, description, entreprise_nom, lieu_travail,
             type_contrat, salaire_libelle, date_creation, url, raw_json, source,
             collected_at)
            VALUES
            (:id, :intitule, :description, :entreprise_nom, :lieu_travail,
             :type_contrat, :salaire_libelle, :date_creation, :url, :raw_json, :source,
             :collected_at)
            """,
            offre_db,
        )
        conn.commit()

    profil_path = os.getenv("PROFILE_PATH", "config/profile.yaml")
    with open(profil_path, encoding="utf-8") as f:
        profil = yaml.safe_load(f)
    profil_texte = formater_profil(profil)

    with get_connection(db_path) as conn:
        offre_row = conn.execute(
            "SELECT * FROM offres WHERE id = ?", (offre_db["id"],)
        ).fetchone()

    score = -1
    if offre_row:
        score, explication, points_forts, points_faibles = scorer_offre(offre_row, profil_texte)
        mettre_a_jour_score(offre_db["id"], score, explication, points_forts, points_faibles, db_path)

    return offre_dict, score


def obtenir_info_entreprise(nom: str):
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "query": f"{nom} entreprise secteur activité",
                "max_results": 1,
                "api_key": api_key,
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0].get("content") or results[0].get("snippet")
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
# Géolocalisation statique
# ─────────────────────────────────────────────

VILLES_COORDS = {
    # France
    "paris": (48.8566, 2.3522),
    "lyon": (45.7640, 4.8357),
    "bordeaux": (44.8378, -0.5792),
    "lille": (50.6292, 3.0573),
    "nantes": (47.2184, -1.5536),
    "toulouse": (43.6047, 1.4442),
    "marseille": (43.2965, 5.3698),
    "strasbourg": (48.5734, 7.7521),
    "rennes": (48.1173, -1.6778),
    "lannion": (48.7325, -3.4595),
    "brest": (48.3905, -4.4860),
    # Belgique
    "bruxelles": (50.8503, 4.3517),
    "brussels": (50.8503, 4.3517),
    "anvers": (51.2194, 4.4025),
    "gand": (51.0543, 3.7174),
    "louvain": (50.8798, 4.7005),
    "liège": (50.6292, 5.5797),
    # Suisse
    "zurich": (47.3769, 8.5417),
    "genève": (46.2044, 6.1432),
    "geneva": (46.2044, 6.1432),
    "lausanne": (46.5197, 6.6323),
    "berne": (46.9481, 7.4474),
    "bâle": (47.5596, 7.5886),
    # Espagne
    "madrid": (40.4168, -3.7038),
    "barcelone": (41.3851, 2.1734),
    "barcelona": (41.3851, 2.1734),
    "valence": (39.4699, -0.3763),
    # Portugal
    "lisbonne": (38.7169, -9.1395),
    "lisbon": (38.7169, -9.1395),
    "porto": (41.1579, -8.6291),
    # Italie
    "milan": (45.4654, 9.1859),
    "rome": (41.9028, 12.4964),
    "turin": (45.0703, 7.6869),
    # Pays-Bas
    "amsterdam": (52.3676, 4.9041),
    "rotterdam": (51.9244, 4.4777),
    # Allemagne
    "berlin": (52.5200, 13.4050),
    "munich": (48.1351, 11.5820),
    "hambourg": (53.5753, 10.0153),
    "frankfurt": (50.1109, 8.6821),
    # UK
    "london": (51.5074, -0.1278),
    "londres": (51.5074, -0.1278),
    # Remote
    "remote": (48.8566, 2.3522),
    "à distance": (48.8566, 2.3522),
    "full remote": (48.8566, 2.3522),
}


def get_coords(lieu: str):
    """Retourne les coordonnées GPS d'une ville depuis le texte de localisation."""
    if not lieu:
        return None
    lieu_lower = lieu.lower()
    for ville, coords in VILLES_COORDS.items():
        if ville in lieu_lower:
            return coords
    return None


def get_couleur_pin(score, statut=None):
    """Couleur du pin selon le statut puis le score."""
    if statut == "Postulé":
        return "blue"
    if statut == "Entretien":
        return "purple"
    if statut == "Refusé":
        return "gray"
    if score is None or score < 0:
        return "gray"
    if score >= 85:
        return "green"
    if score >= 60:
        return "orange"
    return "red"


def afficher_carte(df):
    from folium.plugins import MarkerCluster

    st.subheader("Carte des offres")

    # Filtres statut
    st.markdown("**Afficher les statuts :**")
    col_s1, col_s2, col_s3, col_s4 = st.columns(4)
    show_a_postuler = col_s1.checkbox("📋 À postuler", value=True)
    show_postule = col_s2.checkbox("✅ Postulé", value=True)
    show_entretien = col_s3.checkbox("🎯 Entretien", value=True)
    show_refuse = col_s4.checkbox("❌ Refusé", value=False)

    statuts_affiches = []
    if show_a_postuler:
        statuts_affiches.append("À postuler")
    if show_postule:
        statuts_affiches.append("Postulé")
    if show_entretien:
        statuts_affiches.append("Entretien")
    if show_refuse:
        statuts_affiches.append("Refusé")

    # Légende
    col1, col2, col3, col4 = st.columns(4)
    col1.markdown("🟢 **Prioritaire** (≥ 85)")
    col2.markdown("🟠 **À considérer** (60–84)")
    col3.markdown("🔴 **Faible** (< 60)")
    col4.markdown("🔵 **Postulé** · 🟣 **Entretien**")

    # Tri par score décroissant + limite
    df_carte = df.sort_values("score", ascending=False).head(300)
    if statuts_affiches:
        df_carte = df_carte[df_carte["statut"].isin(statuts_affiches)]

    m = folium.Map(location=[48.0, 8.0], zoom_start=5, tiles="CartoDB positron")
    cluster = MarkerCluster(
        options={"maxClusterRadius": 40, "disableClusteringAtZoom": 8}
    ).add_to(m)

    nb_pins = 0
    for _, row in df_carte.iterrows():
        coords = get_coords(str(row.get("lieu_travail", "") or ""))
        if not coords:
            continue

        score = row.get("score", None)
        statut = row.get("statut", "") or ""
        couleur = get_couleur_pin(score, statut)
        titre = row.get("intitule", "Offre") or "Offre"
        entreprise = row.get("entreprise_nom", "") or ""
        lieu = row.get("lieu_travail", "") or ""
        url = row.get("url", "") or ""
        source = row.get("source", "") or ""

        if score is not None and score >= 85:
            score_color = "#2d6a4f"
        elif score is not None and score >= 60:
            score_color = "#e07b00"
        else:
            score_color = "#cc0000"

        lien_html = f"<a href='{url}' target='_blank'>Voir l'offre</a>" if url else ""
        popup_html = f"""
        <div style="font-family: sans-serif; min-width: 200px;">
            <b style="font-size:14px">{titre}</b><br>
            <span style="color:#555">{entreprise}</span><br>
            <span style="color:#888;font-size:12px">{lieu} · {source}</span><br>
            <b style="color:{score_color}">Score : {score}/100</b><br>
            {lien_html}
        </div>
        """

        offre_id = row.get("id", "") or ""
        folium.Marker(
            location=coords,
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"[{offre_id}] {titre} — {entreprise} ({score}/100)",
            icon=folium.Icon(color=couleur, icon="briefcase", prefix="fa"),
        ).add_to(cluster)
        nb_pins += 1

    st.caption(f"{nb_pins} offres géolocalisées sur {len(df_carte)} filtrées ({len(df)} au total)")
    map_data = st_folium(m, width=None, height=600, returned_objects=["last_object_clicked_tooltip"])

    if map_data and map_data.get("last_object_clicked_tooltip"):
        tooltip_clique = map_data["last_object_clicked_tooltip"]
        match = re.search(r'\[([^\]]+)\]', tooltip_clique)
        if match:
            offre_id_clique = match.group(1)
            with get_connection(DB_PATH) as conn:
                offre_row = conn.execute(
                    "SELECT * FROM offres WHERE id = ?", (offre_id_clique,)
                ).fetchone()
            if offre_row:
                offre = dict(offre_row)
                st.divider()
                st.subheader(f"📌 {offre['intitule']} — {offre['entreprise_nom']}")

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Score", f"{offre['score']}/100")
                col2.markdown(f"**Lieu :** {offre.get('lieu_travail') or '—'}")
                col3.markdown(f"**Contrat :** {offre.get('type_contrat') or '—'}")
                col4.markdown(f"**Source :** {offre.get('source') or '—'}")

                st.markdown(f"**Analyse :** {offre.get('score_explication') or '—'}")

                col_actions1, col_actions2, _ = st.columns([2, 2, 1])

                with col_actions1:
                    statuts_liste = ["À postuler", "Postulé", "Entretien", "Refusé"]
                    statut_actuel = offre.get("statut") or "À postuler"
                    if statut_actuel not in statuts_liste:
                        statut_actuel = "À postuler"
                    nouveau_statut = st.selectbox(
                        "Statut",
                        statuts_liste,
                        index=statuts_liste.index(statut_actuel),
                        key=f"carte_statut_{offre['id']}",
                    )
                    if nouveau_statut != statut_actuel:
                        mettre_a_jour_statut(offre["id"], nouveau_statut, DB_PATH)
                        st.rerun()

                with col_actions2:
                    if offre.get("url"):
                        st.markdown("&nbsp;")
                        st.link_button("🔗 Ouvrir l'offre", offre["url"], use_container_width=True)

                with st.expander("Points clés"):
                    col_pf, col_pfai = st.columns(2)
                    with col_pf:
                        st.markdown("**Points forts**")
                        try:
                            points_forts = json.loads(offre.get("score_points_forts") or "[]")
                            for p in points_forts:
                                st.markdown(f"✅ {p}")
                            if not points_forts:
                                st.write("—")
                        except (json.JSONDecodeError, TypeError):
                            st.write("—")
                    with col_pfai:
                        st.markdown("**Points faibles**")
                        try:
                            points_faibles = json.loads(offre.get("score_points_faibles") or "[]")
                            for p in points_faibles:
                                st.markdown(f"❌ {p}")
                            if not points_faibles:
                                st.write("—")
                        except (json.JSONDecodeError, TypeError):
                            st.write("—")


# ─────────────────────────────────────────────
# App principale
# ─────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="JobAgent — Dashboard",
        page_icon="💼",
        layout="wide",
    )
    st.title("💼 JobAgent — Offres d'emploi")

    init_db(DB_PATH)

    # ── Sidebar ──────────────────────────────
    st.sidebar.header("Filtres")

    score_min = st.sidebar.slider("Score minimum", 0, 100, 60)

    sources_dispo = ["Adzuna", "Indeed", "Apec", "Wttj", "Google", "Manuel", "Linkedin"]
    sources = st.sidebar.multiselect("Source", sources_dispo, default=sources_dispo)

    contrats_dispo = ["CDI", "CDD", "Freelance", "N/A"]
    contrats = st.sidebar.multiselect("Contrat", contrats_dispo, default=contrats_dispo)

    statuts_dispo = ["À postuler", "Postulé", "Refusé", "Entretien"]
    statuts = st.sidebar.multiselect("Statut", statuts_dispo, default=statuts_dispo)

    st.sidebar.divider()
    st.sidebar.markdown("### Ajouter une offre")
    mode_import = st.sidebar.radio(
        "Mode", ["URL", "Texte brut"], horizontal=True, label_visibility="collapsed"
    )
    if mode_import == "URL":
        input_offre = st.sidebar.text_input("URL de l'offre", placeholder="https://...")
    else:
        input_offre = st.sidebar.text_area("Colle le texte de l'offre", height=150)
    btn_importer = st.sidebar.button("Importer", type="primary", use_container_width=True)

    if btn_importer:
        if not input_offre or not input_offre.strip():
            st.sidebar.error("Erreur : champ vide.")
        else:
            with st.sidebar.status("Import en cours…"):
                try:
                    offre_dict, score = importer_offre_manuelle(input_offre.strip(), mode_import, DB_PATH)
                    st.sidebar.success(
                        f"✓ Offre importée et scorée : {offre_dict.get('titre', '')} — Score : {score}/100"
                    )
                    st.rerun()
                except Exception as exc:
                    st.sidebar.error(f"Erreur : {exc}")

    st.sidebar.divider()
    st.sidebar.markdown("### 🗑️ Nettoyage")
    jours_max = st.sidebar.slider("Supprimer les offres de plus de", 7, 90, 30, step=7, format="%d jours")

    if st.sidebar.button("Supprimer les vieilles offres", type="secondary"):
        with get_connection(DB_PATH) as conn:
            nb = conn.execute("""
                SELECT COUNT(*) FROM offres
                WHERE collected_at <= date('now', ? || ' days')
                AND (statut IS NULL OR statut NOT IN ('Postulé', 'Entretien'))
            """, (f"-{jours_max}",)).fetchone()[0]

            if nb == 0:
                st.sidebar.info("Aucune offre à supprimer.")
            else:
                conn.execute("""
                    DELETE FROM offres
                    WHERE collected_at <= date('now', ? || ' days')
                    AND (statut IS NULL OR statut NOT IN ('Postulé', 'Entretien'))
                """, (f"-{jours_max}",))
                conn.commit()
                st.sidebar.success(f"✓ {nb} offres supprimées (> {jours_max} jours, non postulées)")
                st.rerun()

    st.sidebar.divider()

    if st.sidebar.button("Lancer le pipeline", type="primary"):
        with st.sidebar.expander("Logs pipeline", expanded=True):
            log_placeholder = st.empty()
            proc = subprocess.Popen(
                ["python", "src/pipeline.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            lines = []
            for line in proc.stdout:
                lines.append(line.rstrip())
                log_placeholder.code("\n".join(lines[-50:]))
            proc.wait()
            if proc.returncode == 0:
                st.sidebar.success("Pipeline terminé avec succès.")
            else:
                st.sidebar.error(f"Pipeline terminé avec le code {proc.returncode}.")
            st.rerun()

    # ── Chargement et filtrage des données ───
    df_complet = charger_offres(DB_PATH)

    if df_complet.empty:
        st.info("Aucune offre scorée en base. Lancez le pipeline pour collecter des offres.")
        return

    # Colonnes dérivées
    df_complet["score"] = df_complet["score"].fillna(0).astype(int)
    df_complet["Source"] = df_complet["source"].str.capitalize().fillna("Inconnu")
    df_complet["statut"] = df_complet["statut"].fillna("À postuler")

    # Normalisation contrat pour le filtre
    def normaliser_contrat(val):
        val = (val or "").strip()
        if not val:
            return "N/A"
        return val

    df_complet["_contrat_norm"] = df_complet["type_contrat"].apply(normaliser_contrat)

    # Application des filtres
    sources_lower = [s.lower() for s in sources]
    mask = (
        (df_complet["score"] >= score_min)
        & (df_complet["source"].str.lower().isin(sources_lower))
        & (df_complet["statut"].isin(statuts))
    )

    # Filtre contrat : "N/A" couvre les valeurs vides
    if "N/A" in contrats:
        contrats_autres = [c for c in contrats if c != "N/A"]
        mask_contrat = (
            df_complet["_contrat_norm"].isin(contrats_autres)
            | (df_complet["_contrat_norm"] == "N/A")
        )
    else:
        mask_contrat = df_complet["_contrat_norm"].isin(contrats)
    mask = mask & mask_contrat

    df_filtre = df_complet[mask].reset_index(drop=True)

    # ── Onglets principaux ────────────────────
    onglet_dashboard, onglet_carte = st.tabs(["📋 Dashboard", "🗺️ Carte des offres"])

    with onglet_carte:
        afficher_carte(df_filtre)

    with onglet_dashboard:
        # ── Métriques ────────────────────────────
        total = len(df_complet)
        prioritaires = int((df_complet["score"] >= 85).sum())
        postulees = int((df_complet["statut"] == "Postulé").sum())
        score_moyen = df_complet["score"].mean()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total offres scorées", total)
        m2.metric("Prioritaires (≥ 85)", prioritaires)
        m3.metric("Postulées", postulees)
        m4.metric("Score moyen", f"{score_moyen:.1f}" if not pd.isna(score_moyen) else "—")

        st.divider()

        # ── DataFrames par statut ─────────────────
        if df_filtre.empty:
            st.warning("Aucune offre ne correspond aux filtres sélectionnés.")
            return

        # Colonne date_postulation : vide si non postulée
        if "date_postulation" not in df_filtre.columns:
            df_filtre["date_postulation"] = ""
        df_filtre["date_postulation"] = df_filtre["date_postulation"].fillna("")

        if "collected_at" not in df_filtre.columns:
            df_filtre["collected_at"] = ""
        df_filtre["collected_at"] = df_filtre["collected_at"].fillna("")

        cols_affichage = ["score", "intitule", "entreprise_nom",
                          "lieu_travail", "type_contrat", "salaire_libelle",
                          "Source", "statut", "date_postulation", "collected_at", "url"]
        rename_map = {
            "score": "Score",
            "intitule": "Poste",
            "entreprise_nom": "Entreprise",
            "lieu_travail": "Lieu",
            "type_contrat": "Contrat",
            "salaire_libelle": "Salaire",
            "statut": "Statut",
            "date_postulation": "Postulé le",
            "collected_at": "Ajouté le",
            "url": "URL",
        }

        st.markdown("""
<style>
[data-testid="stDataFrame"] td { color: var(--text-color) !important; }
</style>
""", unsafe_allow_html=True)

        def build_styled(df_tab):
            df_aff = df_tab[cols_affichage].rename(columns=rename_map)
            return (
                df_aff.style
                .apply(colorier_texte, axis=1)
                .map(lambda _: "font-weight: bold", subset=["Score"])
            )

        col_config = {"URL": st.column_config.LinkColumn("URL", display_text="Lien")}
        dataframe_kwargs = dict(
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            column_config=col_config,
        )

        # ── DataFrames par statut (avant st.tabs pour les compteurs) ─────────
        df_a_postuler = df_filtre[df_filtre["statut"].isin(["À postuler", "", None]) | df_filtre["statut"].isna()].reset_index(drop=True)
        df_postule = df_filtre[df_filtre["statut"] == "Postulé"].reset_index(drop=True)
        df_entretien = df_filtre[df_filtre["statut"] == "Entretien"].reset_index(drop=True)
        df_refuse = df_filtre[df_filtre["statut"] == "Refusé"].reset_index(drop=True)

        # ── Onglets statut ────────────────────────
        onglet_a_postuler, onglet_postule, onglet_entretien, onglet_refuse = st.tabs([
            f"📋 À postuler ({len(df_a_postuler)})",
            f"✅ Postulé ({len(df_postule)})",
            f"🎯 Entretien ({len(df_entretien)})",
            f"❌ Refusé ({len(df_refuse)})",
        ])

        with onglet_a_postuler:
            selection_a_postuler = st.dataframe(build_styled(df_a_postuler), key="table_a_postuler", **dataframe_kwargs)

        with onglet_postule:
            selection_postule = st.dataframe(build_styled(df_postule), key="table_postule", **dataframe_kwargs)

        with onglet_entretien:
            selection_entretien = st.dataframe(build_styled(df_entretien), key="table_entretien", **dataframe_kwargs)

        with onglet_refuse:
            selection_refuse = st.dataframe(build_styled(df_refuse), key="table_refuse", **dataframe_kwargs)

        # ── Panneau détail ────────────────────────
        offre_selectionnee = None
        if selection_a_postuler.selection.rows:
            idx = selection_a_postuler.selection.rows[0]
            offre_selectionnee = df_a_postuler.iloc[idx]
        elif selection_postule.selection.rows:
            idx = selection_postule.selection.rows[0]
            offre_selectionnee = df_postule.iloc[idx]
        elif selection_entretien.selection.rows:
            idx = selection_entretien.selection.rows[0]
            offre_selectionnee = df_entretien.iloc[idx]
        elif selection_refuse.selection.rows:
            idx = selection_refuse.selection.rows[0]
            offre_selectionnee = df_refuse.iloc[idx]

        if offre_selectionnee is not None:
            offre = offre_selectionnee

            st.divider()
            st.subheader(f"Détail — {offre.get('intitule', '')}")

            col_gauche, col_droite = st.columns([3, 1])

            with col_gauche:
                st.markdown(f"**Entreprise :** {offre.get('entreprise_nom') or '—'}  \n"
                            f"**Lieu :** {offre.get('lieu_travail') or '—'}  \n"
                            f"**Contrat :** {offre.get('type_contrat') or '—'}  \n"
                            f"**Salaire :** {offre.get('salaire_libelle') or '—'}  \n"
                            f"**Score :** {offre.get('score')}/100")

                with st.expander("Analyse complète", expanded=True):
                    st.write(offre.get("score_explication") or "—")

                col_pf, col_pfai = st.columns(2)
                with col_pf:
                    st.markdown("**Points forts**")
                    try:
                        points_forts = json.loads(offre.get("score_points_forts") or "[]")
                        for p in points_forts:
                            st.markdown(f"✅ {p}")
                        if not points_forts:
                            st.write("—")
                    except (json.JSONDecodeError, TypeError):
                        st.write("—")

                with col_pfai:
                    st.markdown("**Points faibles**")
                    try:
                        points_faibles = json.loads(offre.get("score_points_faibles") or "[]")
                        for p in points_faibles:
                            st.markdown(f"❌ {p}")
                        if not points_faibles:
                            st.write("—")
                    except (json.JSONDecodeError, TypeError):
                        st.write("—")

            with col_droite:
                st.markdown("**Actions**")

                # Changement de statut
                statuts_liste = ["À postuler", "Postulé", "Refusé", "Entretien"]
                statut_actuel = offre.get("statut") or "À postuler"
                if statut_actuel not in statuts_liste:
                    statut_actuel = "À postuler"

                nouveau_statut = st.selectbox(
                    "Statut candidature",
                    statuts_liste,
                    index=statuts_liste.index(statut_actuel),
                    key=f"statut_{offre['id']}",
                )
                if nouveau_statut != statut_actuel:
                    mettre_a_jour_statut(offre["id"], nouveau_statut, DB_PATH)
                    st.rerun()

                # Ouvrir l'offre
                url_offre = offre.get("url") or ""
                if url_offre:
                    st.link_button("Ouvrir l'offre", url_offre, use_container_width=True)

                # Adapter le CV
                if st.button("Postuler (adapter CV)", key=f"cv_{offre['id']}", use_container_width=True):
                    with st.spinner("Adaptation du CV en cours..."):
                        result = subprocess.run(
                            ["python", "src/cv_adapter.py", "--offre-id", offre["id"]],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                        )
                    if result.returncode == 0:
                        offre_id_court = offre["id"].replace("adzuna_", "")[:12]
                        cv_path = os.path.join("cv", f"cv_adapte_{offre_id_court}.html")
                        st.success(f"CV adapté généré : {cv_path}")
                    else:
                        st.error(result.stderr or "Erreur lors de l'adaptation du CV.")

            st.write("DEBUG: section enrichissement visible")

            # ── Enrichissement offre ──────────────────
            with st.expander("📋 Enrichir l'offre avec la description complète"):
                st.caption("Colle ici la description complète de l'offre pour améliorer le scoring et l'adaptation du CV.")
                description_complete = st.text_area(
                    "Description complète",
                    value=offre.get("description", "") or "",
                    height=300,
                    key=f"desc_enrichie_{offre['id']}",
                )
                if st.button("💾 Sauvegarder et rescorer", key=f"btn_enrichir_{offre['id']}"):
                    if description_complete.strip():
                        with get_connection(DB_PATH) as conn:
                            conn.execute(
                                "UPDATE offres SET description = ? WHERE id = ?",
                                (description_complete.strip(), offre["id"]),
                            )
                            conn.commit()

                        from scorer import scorer_offre, mettre_a_jour_score, formater_profil
                        import yaml
                        from langchain_google_genai import ChatGoogleGenerativeAI

                        api_key = os.getenv("GOOGLE_AI_STUDIO_KEY")
                        profil_path = os.getenv("PROFILE_PATH", "config/profile.yaml")
                        with open(profil_path, encoding="utf-8") as f:
                            profil = yaml.safe_load(f)
                        profil_texte = formater_profil(profil)

                        with get_connection(DB_PATH) as conn:
                            offre_row = conn.execute(
                                "SELECT * FROM offres WHERE id = ?", (offre["id"],)
                            ).fetchone()

                        if offre_row and api_key:
                            llm = ChatGoogleGenerativeAI(
                                model="gemini-3.1-flash-lite-preview",
                                google_api_key=api_key,
                                temperature=0.2,
                                max_output_tokens=1024,
                            )
                            score, explication, points_forts, points_faibles = scorer_offre(
                                offre_row, profil_texte, llm
                            )
                            mettre_a_jour_score(
                                offre["id"], score, explication, points_forts, points_faibles, DB_PATH
                            )
                            st.success(f"✓ Offre enrichie et rescorée : {score}/100")
                            st.rerun()
                        elif not api_key:
                            st.error("GOOGLE_AI_STUDIO_KEY manquante dans .env")
                    else:
                        st.warning("La description est vide.")

            # ── Zone dangereuse ───────────────────────
            st.divider()
            confirm_key = f"confirm_del_{offre['id']}"
            if st.button("Supprimer cette offre", type="secondary", key=f"del_{offre['id']}"):
                st.session_state[confirm_key] = True

            if st.session_state.get(confirm_key):
                st.warning("Confirmer la suppression ?")
                if st.button("Confirmer", key=f"confirm_{offre['id']}"):
                    with get_connection(DB_PATH) as conn:
                        conn.execute("DELETE FROM offres WHERE id = ?", (offre["id"],))
                        conn.commit()
                    st.session_state.pop(confirm_key, None)
                    st.rerun()


if __name__ == "__main__":
    main()
