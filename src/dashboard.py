"""
Dashboard Streamlit — Consultation et suivi des offres d'emploi.

Utilisation :
    streamlit run src/dashboard.py

Conserve la fonction exporter_txt() utilisée par pipeline.py.
"""

import json
import os
import subprocess
import sys
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Ajouter src/ au path pour importer db.py
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection

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

        if score >= 80:
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
    if "adzuna" in url:
        return "Adzuna"
    if "apec" in url:
        return "APEC"
    if "indeed" in url:
        return "Indeed"
    return "N/A"


def mettre_a_jour_statut(offre_id: str, statut: str, db_path: str) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE offres SET statut = ? WHERE id = ?",
            (statut, offre_id),
        )
        conn.commit()


def deriver_priorite(score: int) -> str:
    if score >= 85:
        return "★★★ PRIORITAIRE"
    if score >= 60:
        return "★★ À CONSIDÉRER"
    return "★ FAIBLE"


def charger_offres(db_path: str) -> pd.DataFrame:
    """Charge toutes les offres scorées depuis la DB."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM offres WHERE score IS NOT NULL AND score >= 0 ORDER BY score DESC"
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

    sources_dispo = ["Adzuna", "Indeed", "APEC"]
    sources = st.sidebar.multiselect("Source", sources_dispo, default=sources_dispo)

    contrats_dispo = ["CDI", "CDD", "Freelance", "N/A"]
    contrats = st.sidebar.multiselect("Contrat", contrats_dispo, default=contrats_dispo)

    statuts_dispo = ["À postuler", "Postulé", "Refusé", "Entretien"]
    statuts = st.sidebar.multiselect("Statut", statuts_dispo, default=statuts_dispo)

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
    df_complet["Source"] = df_complet["url"].apply(deriver_source)
    df_complet["Priorité"] = df_complet["score"].apply(deriver_priorite)
    df_complet["statut"] = df_complet["statut"].fillna("À postuler")

    # Normalisation contrat pour le filtre
    def normaliser_contrat(val):
        val = (val or "").strip()
        if not val:
            return "N/A"
        return val

    df_complet["_contrat_norm"] = df_complet["type_contrat"].apply(normaliser_contrat)

    # Application des filtres
    mask = (
        (df_complet["score"] >= score_min)
        & (df_complet["Source"].isin(sources))
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

    # ── Tableau principal ─────────────────────
    st.subheader(f"Offres ({len(df_filtre)} résultats)")

    if df_filtre.empty:
        st.warning("Aucune offre ne correspond aux filtres sélectionnés.")
        return

    cols_affichage = ["score", "Priorité", "intitule", "entreprise_nom",
                      "lieu_travail", "type_contrat", "salaire_libelle",
                      "Source", "statut", "url"]
    rename_map = {
        "score": "Score",
        "intitule": "Poste",
        "entreprise_nom": "Entreprise",
        "lieu_travail": "Lieu",
        "type_contrat": "Contrat",
        "salaire_libelle": "Salaire",
        "statut": "Statut",
        "url": "URL",
    }

    df_affichage = df_filtre[cols_affichage].rename(columns=rename_map)

    st.markdown("""
<style>
[data-testid="stDataFrame"] td { color: var(--text-color) !important; }
</style>
""", unsafe_allow_html=True)

    styled = (
        df_affichage.style
        .apply(colorier_texte, axis=1)
        .map(lambda _: "font-weight: bold", subset=["Score"])
    )

    selection = st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "URL": st.column_config.LinkColumn("URL", display_text="Lien"),
        },
    )

    # ── Panneau détail ────────────────────────
    if selection.selection.rows:
        idx = selection.selection.rows[0]
        offre = df_filtre.iloc[idx]

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

            st.divider()

            # Suppression de l'offre
            confirm_key = f"confirm_del_{offre['id']}"
            if st.button("Supprimer cette offre", type="secondary", key=f"del_{offre['id']}", use_container_width=True):
                st.session_state[confirm_key] = True

            if st.session_state.get(confirm_key):
                st.warning("Confirmer la suppression ?")
                if st.button("Confirmer", key=f"confirm_{offre['id']}", use_container_width=True):
                    with get_connection(DB_PATH) as conn:
                        conn.execute("DELETE FROM offres WHERE id = ?", (offre["id"],))
                        conn.commit()
                    st.session_state.pop(confirm_key, None)
                    st.rerun()


if __name__ == "__main__":
    main()
