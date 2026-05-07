"""
Scorer d'offres d'emploi via Anthropic Claude Haiku.

Utilisation :
    python src/scorer.py              # Score les 20 prochaines offres non scorées
    python src/scorer.py --limite 50  # Score jusqu'à 50 offres
    python src/scorer.py --rescorer   # Rescorer les offres en erreur (score=-1)
"""

import argparse
import json
import logging
import os
import sys
import time

import anthropic
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn

# Ajouter le dossier src/ au path pour importer db.py
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection

load_dotenv(override=True)

console = Console()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ─────────────────────────────────────────────
# Modèle Pydantic — structure de la réponse attendue
#
# Avant : on demandait au LLM de renvoyer du JSON, on parsait à la main.
# Maintenant : on déclare un modèle Python, LangChain transmet le schéma
# au LLM et valide la réponse automatiquement. Si le LLM renvoie
# un score hors de [0,100], Pydantic lève une erreur immédiatement.
# ─────────────────────────────────────────────

class ScoringResult(BaseModel):
    score: int = Field(ge=0, le=100, description="Score de correspondance entre 0 et 100")
    explication: str = Field(description="Résumé en 2-3 phrases de la correspondance globale")
    points_forts: list[str] = Field(default_factory=list, description="Points forts de la candidature")
    points_faibles: list[str] = Field(default_factory=list, description="Points faibles ou manques")


# ─────────────────────────────────────────────
# Prompt système
# Note : on retire le bloc JSON explicite — LangChain injecte
# automatiquement le schéma Pydantic dans les instructions au LLM.
# ─────────────────────────────────────────────

SYSTEM_PROMPT_SCORING = """Tu es un expert en recrutement et en matching CV/offre d'emploi.
Tu analyses la correspondance entre un profil candidat et une offre d'emploi.

Barème de scoring :
- 90-100 : Correspondance parfaite, candidature prioritaire
- 70-89  : Bonne correspondance, à candidater rapidement
- 50-69  : Correspondance partielle, possible avec une lettre ciblée
- 30-49  : Correspondance faible, effort significatif requis
- 0-29   : Inadapté, ne pas candidater

Critères d'évaluation (par ordre d'importance) :
1. Adéquation du poste avec le titre recherché
2. Correspondance des compétences techniques
3. Type de contrat et conditions (salaire, télétravail)
4. Secteur et culture d'entreprise
5. Localisation"""


# ─────────────────────────────────────────────
# Formatage du profil et de l'offre
# ─────────────────────────────────────────────

def formater_profil(profil: dict) -> str:
    """Formate le profil candidat en texte structuré."""
    candidat = profil.get("candidat", {})
    competences = profil.get("competences", {})
    criteres = profil.get("criteres", {})
    prefs = profil.get("preferences_entreprise", {})
    experience = profil.get("experience", {})
    formation = profil.get("formation", {})

    lignes = [
        "## PROFIL CANDIDAT",
        f"Poste cible : {candidat.get('poste_cible', 'Non précisé')}",
        f"Expérience : {experience.get('annees_totales', '?')} ans",
        f"Formation : {formation.get('niveau', '')} en {', '.join(formation.get('domaines', []))}",
        f"Domaines d'expérience : {', '.join(experience.get('domaines', []))}",
        f"Compétences techniques : {', '.join(competences.get('techniques', []))}",
        f"Soft skills : {', '.join(competences.get('soft_skills', []))}",
        "",
        "## CRITÈRES",
        f"Types de contrat acceptés : {', '.join(criteres.get('types_contrat', []))}",
        f"Salaire minimum : {criteres.get('salaire_min_annuel', 'Non précisé')} €/an brut",
        f"Télétravail souhaité : {'Oui' if criteres.get('teletravail_souhaite') else 'Non'}",
        f"Télétravail minimum : {criteres.get('teletravail_minimum_jours_par_semaine', 0)} jours/semaine",
        f"Localisation acceptée : {', '.join(criteres.get('localisations_acceptees', ['Paris / Île-de-France']))}",
        f"Secteurs préférés : {', '.join(criteres.get('secteurs_preferes', []))}",
        "",
        "## PRÉFÉRENCES ENTREPRISE",
        f"Taille : {prefs.get('taille_preferee', 'Indifférent')}",
        f"Culture : {', '.join(prefs.get('culture', []))}",
        f"Stack préférée : {', '.join(prefs.get('stack_preferee', []))}",
    ]

    return "\n".join(lignes)


def formater_offre(offre: dict) -> str:
    """Formate une offre d'emploi en texte structuré."""
    lignes = [
        "## OFFRE D'EMPLOI",
        f"Titre : {offre['intitule'] or 'Non précisé'}",
        f"Entreprise : {offre['entreprise_nom'] or 'Non précisée'}",
        f"Lieu : {offre['lieu_travail'] or 'Non précisé'}",
        f"Type de contrat : {offre['type_contrat'] or 'Non précisé'}",
        f"Salaire : {offre['salaire_libelle'] or 'Non précisé'}",
        "",
        "### Description de l'offre",
        (offre['description'] or 'Aucune description')[:3000],
    ]

    return "\n".join(lignes)


# ─────────────────────────────────────────────
# Appel Anthropic Claude Haiku
#
# Le prompt demande explicitement un JSON conforme au schéma ScoringResult.
# On parse la réponse avec json.loads puis on valide via Pydantic.
# ─────────────────────────────────────────────

SCHEMA_JSON_SCORING = """Réponds UNIQUEMENT avec un objet JSON strict — aucun texte avant/après, pas de backticks — de la forme :
{
  "score": <entier entre 0 et 100>,
  "explication": "<résumé en 2-3 phrases>",
  "points_forts": ["<point fort 1>", "..."],
  "points_faibles": ["<point faible 1>", "..."]
}"""


def scorer_offre(
    offre: dict,
    profil_texte: str,
) -> tuple[int, str, list, list]:
    """
    Score une offre via Claude Haiku.
    Retourne (score, explication, points_forts, points_faibles).
    """
    offre_dict = dict(offre)
    description = offre_dict.get("description", "") or ""
    source = (offre_dict.get("source") or "").lower()
    est_linkedin_sans_details = "linkedin" in source and len(description) < 200

    if est_linkedin_sans_details:
        offre_texte = "\n".join([
            "## OFFRE D'EMPLOI (LinkedIn — description non disponible)",
            f"Titre : {offre_dict.get('intitule') or 'Non précisé'}",
            f"Entreprise : {offre_dict.get('entreprise_nom') or 'Non précisée'}",
            f"Lieu : {offre_dict.get('lieu_travail') or 'Non précisé'}",
            f"Type de contrat : {offre_dict.get('type_contrat') or 'Non précisé'}",
            f"Salaire : {offre_dict.get('salaire_libelle') or 'Non précisé'}",
            "",
            "Note : la description complète n'est pas disponible. Score basé sur le titre et les métadonnées uniquement.",
        ])
    else:
        offre_texte = formater_offre(offre_dict)

    prompt = f"{profil_texte}\n\n---\n\n{offre_texte}"

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT_SCORING,
            messages=[{"role": "user", "content": f"{prompt}\n\n{SCHEMA_JSON_SCORING}"}]
        )
        texte_brut = response.content[0].text if response.content else ""
        logging.info("=== HAIKU REPONSE BRUTE === %s", repr(texte_brut[:500]))
        texte = texte_brut

        # Extraire l'objet JSON (robuste aux préfixes éventuels)
        debut = texte.find("{")
        fin = texte.rfind("}")
        if debut >= 0 and fin > debut:
            texte = texte[debut:fin + 1]

        donnees = json.loads(texte)
        result = ScoringResult(**donnees)
        return result.score, result.explication, result.points_forts, result.points_faibles

    except Exception as e:
        logging.warning("Erreur scoring %s : %s", offre_dict.get("id", "?"), e)
        return 0, f"Scoring échoué : {e}", [], []


# ─────────────────────────────────────────────
# Mise à jour de la base de données
# ─────────────────────────────────────────────

def mettre_a_jour_score(
    offre_id: str,
    score: int,
    explication: str,
    points_forts: list,
    points_faibles: list,
    db_path: str,
) -> None:
    """Met à jour le score et les explications d'une offre dans SQLite."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE offres
            SET score = ?,
                score_explication = ?,
                score_points_forts = ?,
                score_points_faibles = ?,
                score_date = datetime('now')
            WHERE id = ?
            """,
            (
                score,
                explication,
                json.dumps(points_forts, ensure_ascii=False),
                json.dumps(points_faibles, ensure_ascii=False),
                offre_id,
            ),
        )
        conn.commit()


# ─────────────────────────────────────────────
# Point d'entrée principal
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Score les offres d'emploi avec Claude Haiku")
    parser.add_argument("--limite", type=int, default=20, help="Nombre max d'offres à scorer (défaut : 20)")
    parser.add_argument("--rescorer", action="store_true", help="Rescorer les offres avec score=-1")
    args = parser.parse_args()

    console.print("\n[bold cyan]Agent de Recherche d'Emploi — Scoring (Claude Haiku)[/bold cyan]")
    console.print("━" * 50)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]Erreur :[/red] ANTHROPIC_API_KEY manquante dans .env")
        raise SystemExit(1)

    profil_path = os.getenv("PROFILE_PATH", "config/profile.yaml")
    with open(profil_path, encoding="utf-8") as f:
        profil = yaml.safe_load(f)

    db_path = os.getenv("DB_PATH", "data/offers.db")
    init_db(db_path)

    condition = "score IS NULL"
    if args.rescorer:
        condition = "(score IS NULL OR score = -1)"

    with get_connection(db_path) as conn:
        offres = conn.execute(
            f"SELECT * FROM offres WHERE {condition} ORDER BY collected_at DESC LIMIT ?",
            (args.limite,),
        ).fetchall()

    if not offres:
        console.print("[yellow]Aucune offre à scorer.[/yellow]")
        console.print("[dim]Lancez d'abord : python src/collector.py[/dim]")
        return

    console.print(f"[cyan]{len(offres)} offres à scorer[/cyan] (Claude Haiku 4.5)\n")

    profil_texte = formater_profil(profil)
    scores_ok = 0
    scores_erreur = 0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        tache = progress.add_task("Scoring en cours...", total=len(offres))

        for offre in offres:
            offre_dict = dict(offre)
            intitule_court = (offre_dict.get("intitule") or "")[:40]
            progress.update(tache, description=f"Scoring : [italic]{intitule_court}[/italic]")

            score, explication, points_forts, points_faibles = scorer_offre(
                offre, profil_texte
            )

            mettre_a_jour_score(
                offre_dict["id"], score, explication, points_forts, points_faibles, db_path
            )

            if score >= 0:
                scores_ok += 1
            else:
                scores_erreur += 1

            progress.advance(tache)
            #time.sleep(1)

    console.print()
    console.print(f"[green]✓[/green] {scores_ok} offres scorées avec succès")
    if scores_erreur:
        console.print(f"[red]✗[/red] {scores_erreur} erreurs (relancer avec --rescorer)")
    console.print()
    console.print("[dim]Prochaine étape : python src/dashboard.py[/dim]")


if __name__ == "__main__":
    main()
