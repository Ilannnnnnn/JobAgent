"""
Scorer d'offres d'emploi via Google AI Studio (Gemini).

Utilisation :
    python src/scorer.py              # Score les 20 prochaines offres non scorées
    python src/scorer.py --limite 50  # Score jusqu'à 50 offres
    python src/scorer.py --rescorer   # Rescorer les offres en erreur (score=-1)

Ce script :
1. Récupère les offres non scorées depuis SQLite
2. Envoie chaque offre + profil à Gemini pour évaluation
3. Met à jour le score et l'explication dans la base de données
"""

import argparse
import json
import os
import sys
import time

from google import genai
from google.genai import types
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn

# Ajouter le dossier src/ au path pour importer db.py
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection

load_dotenv()

console = Console()

# ─────────────────────────────────────────────
# Prompt système
# ─────────────────────────────────────────────

SYSTEM_PROMPT_SCORING = """Tu es un expert en recrutement et en matching CV/offre d'emploi.
Tu analyses la correspondance entre un profil candidat et une offre d'emploi.

Tu réponds UNIQUEMENT avec du JSON valide dans ce format exact :
{
  "score": <entier entre 0 et 100>,
  "explication": "<résumé en 2-3 phrases de la correspondance globale>",
  "points_forts": ["<point 1>", "<point 2>", "<point 3>"],
  "points_faibles": ["<point 1>", "<point 2>"]
}

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
    """Formate le profil candidat en texte structuré pour Gemini."""
    candidat = profil.get("candidat", {})
    competences = profil.get("competences", {})
    criteres = profil.get("criteres", {})
    prefs = profil.get("preferences_entreprise", {})
    experience = profil.get("experience", {})

    lignes = [
        "## PROFIL CANDIDAT",
        f"Poste cible : {candidat.get('poste_cible', 'Non précisé')}",
        f"Expérience : {experience.get('annees_totales', '?')} ans",
        f"Compétences techniques : {', '.join(competences.get('techniques', []))}",
        f"Soft skills : {', '.join(competences.get('soft_skills', []))}",
        "",
        "## CRITÈRES",
        f"Types de contrat acceptés : {', '.join(criteres.get('types_contrat', []))}",
        f"Salaire minimum : {criteres.get('salaire_min_annuel', 'Non précisé')} €/an brut",
        f"Télétravail souhaité : {'Oui' if criteres.get('teletravail_souhaite') else 'Non'}",
        f"Localisation : Paris / Île-de-France (rayon {criteres.get('distance_km', 30)} km)",
        f"Secteurs préférés : {', '.join(criteres.get('secteurs_preferes', []))}",
        "",
        "## PRÉFÉRENCES ENTREPRISE",
        f"Taille : {prefs.get('taille_preferee', 'Indifférent')}",
        f"Culture : {', '.join(prefs.get('culture', []))}",
    ]

    return "\n".join(lignes)


def formater_offre(offre: dict) -> str:
    """Formate une offre d'emploi en texte structuré pour Gemini."""
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
# Appel Gemini API
# ─────────────────────────────────────────────

def scorer_offre(
    offre: dict,
    profil_texte: str,
    client: genai.Client,
) -> tuple[int, str, list, list]:
    """
    Envoie une offre + profil à Gemini et retourne (score, explication, points_forts, points_faibles).
    Le JSON est garanti par response_mime_type — pas besoin de parser du texte brut.
    En cas d'erreur, retourne score=-1.
    """
    offre_texte = formater_offre(dict(offre))
    prompt = f"{profil_texte}\n\n---\n\n{offre_texte}"

    try:
        reponse = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_SCORING,
                response_mime_type="application/json",
                max_output_tokens=1024,
                temperature=0.2,
            ),
        )
        donnees = json.loads(reponse.text)

        score = int(donnees.get("score", -1))
        explication = donnees.get("explication", "")
        points_forts = donnees.get("points_forts", [])
        points_faibles = donnees.get("points_faibles", [])

        return score, explication, points_forts, points_faibles

    except (json.JSONDecodeError, ValueError, KeyError, Exception) as e:
        console.print(f"  [red]Erreur pour l'offre {dict(offre)['id']} :[/red] {e}")
        return -1, f"Erreur : {str(e)}", [], []


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
    parser = argparse.ArgumentParser(description="Score les offres d'emploi avec Gemini")
    parser.add_argument(
        "--limite",
        type=int,
        default=20,
        help="Nombre maximum d'offres à scorer (défaut : 20)",
    )
    parser.add_argument(
        "--rescorer",
        action="store_true",
        help="Rescorer aussi les offres avec score=-1 (erreurs précédentes)",
    )
    args = parser.parse_args()

    console.print("\n[bold cyan]Agent de Recherche d'Emploi — Scoring (Gemini)[/bold cyan]")
    console.print("━" * 50)

    # Vérifier et configurer la clé API Google
    api_key = os.getenv("GOOGLE_AI_STUDIO_KEY")
    if not api_key:
        console.print("[red]Erreur :[/red] GOOGLE_AI_STUDIO_KEY manquante dans .env")
        console.print("[dim]Obtenir une clé : https://aistudio.google.com/app/apikey[/dim]")
        raise SystemExit(1)

    client = genai.Client(api_key=api_key)

    # Charger le profil
    profil_path = os.getenv("PROFILE_PATH", "config/profile.yaml")
    with open(profil_path, encoding="utf-8") as f:
        profil = yaml.safe_load(f)

    db_path = os.getenv("DB_PATH", "data/offers.db")
    init_db(db_path)

    # Récupérer les offres à scorer
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

    console.print(f"[cyan]{len(offres)} offres à scorer[/cyan] (modèle : gemini-3.1-flash-lite-preview)\n")

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
                offre, profil_texte, client
            )

            mettre_a_jour_score(
                offre_dict["id"], score, explication, points_forts, points_faibles, db_path
            )

            if score >= 0:
                scores_ok += 1
            else:
                scores_erreur += 1

            progress.advance(tache)
            # Pause pour respecter les rate limits (15 req/min sur tier gratuit)
            time.sleep(4)

    # Résumé
    console.print()
    console.print(f"[green]✓[/green] {scores_ok} offres scorées avec succès")
    if scores_erreur:
        console.print(f"[red]✗[/red] {scores_erreur} erreurs (relancer avec --rescorer)")
    console.print()
    console.print("[dim]Prochaine étape : python src/dashboard.py[/dim]")


if __name__ == "__main__":
    main()
