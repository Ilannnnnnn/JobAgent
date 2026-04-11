"""
Adaptateur de CV HTML pour une offre d'emploi spécifique.

Utilisation :
    python src/cv_adapter.py --offre-id <ID_OFFRE>
    python src/cv_adapter.py --offre-id <ID> --output cv/mon_cv_adapte.html

Ce script :
1. Lit l'offre ciblée depuis la base SQLite
2. Lit le CV de base au format HTML
3. Demande à Gemini d'adapter le contenu textuel pour cette offre
4. Génère un fichier HTML adapté — ouvrir dans le navigateur et imprimer en A4

Le HTML de base contient déjà le CSS @media print pour un rendu A4 parfait.

Changement vs version précédente :
    - google-genai SDK brut → LangChain (ChatGoogleGenerativeAI)
    - generate_content_stream() → llm.stream() — même comportement, API unifiée
"""

import argparse
import os
import re
import sys

from bs4 import BeautifulSoup
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

# Ajouter le dossier src/ au path pour importer db.py
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection

load_dotenv()

console = Console()


# ─────────────────────────────────────────────
# Lecture du CV HTML
# ─────────────────────────────────────────────

def lire_cv_html(chemin_html: str) -> str:
    """Lit le fichier HTML du CV de base."""
    if not os.path.exists(chemin_html):
        console.print(f"[red]Erreur :[/red] CV introuvable : {chemin_html}")
        console.print("[dim]Placez votre CV dans cv/cv_base.html[/dim]")
        raise SystemExit(1)

    with open(chemin_html, encoding="utf-8") as f:
        return f.read()


def extraire_contenu_textuel(html: str) -> str:
    """
    Extrait le contenu textuel structuré du HTML pour le contexte Gemini.
    Utilisé uniquement pour montrer à Gemini ce qui peut être modifié.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Supprimer les balises style et script
    for tag in soup(["style", "script"]):
        tag.decompose()

    return soup.get_text(separator="\n", strip=True)


# ─────────────────────────────────────────────
# Prompt Gemini
# ─────────────────────────────────────────────

SYSTEM_PROMPT_CV = """Tu es un expert en rédaction de CV et en stratégie de candidature.
Tu adaptes des CV HTML existants pour maximiser leur impact sur une offre d'emploi spécifique.

RÈGLES ABSOLUES :
- Retourner le HTML COMPLET et VALIDE — du <!DOCTYPE html> jusqu'au </html>
- Ne JAMAIS modifier le CSS, les classes, les IDs, la structure des balises
- Ne JAMAIS inventer de compétences, d'expériences ou de formations absentes du CV original
- Conserver toutes les informations factuelles (dates, noms d'entreprises, diplômes, chiffres)
- Modifier UNIQUEMENT le texte à l'intérieur des balises (jamais les attributs)

CE QUE TU PEUX ADAPTER :
- Le titre du poste (div.cv-title) — aligner avec l'offre
- L'accroche (div.cv-accroche) — reformuler pour cibler l'offre
- Les bullet points (li dans cv-entry-bullets) — réordonner et reformuler pour mettre en avant ce qui est pertinent
- La liste des compétences — réordonner pour placer en premier ce qui correspond à l'offre

CE QUE TU NE DOIS PAS TOUCHER :
- Le nom (div.cv-name)
- Les informations de contact (div.cv-contact)
- Les dates, entreprises, diplômes
- Tout le CSS et les attributs HTML
- La navigation (nav.cv-nav)

IMPORTANT : Répondre UNIQUEMENT avec le HTML complet, sans aucun texte avant ou après, sans bloc markdown."""


def construire_prompt(cv_html: str, offre: dict) -> str:
    """Construit le prompt envoyé à Gemini."""
    return f"""## OFFRE D'EMPLOI CIBLE
Titre : {offre['intitule'] or 'Non précisé'}
Entreprise : {offre['entreprise_nom'] or 'Non précisée'}
Lieu : {offre['lieu_travail'] or 'Non précisé'}
Contrat : {offre['type_contrat'] or 'Non précisé'}
Salaire : {offre['salaire_libelle'] or 'Non précisé'}

### Description complète
{(offre['description'] or 'Aucune description')[:3000]}

---

## CV HTML À ADAPTER
{cv_html}

---

Adapte ce CV HTML pour cette offre en respectant toutes les règles.
Retourne le HTML complet adapté."""


# ─────────────────────────────────────────────
# Appel Gemini avec streaming
# ─────────────────────────────────────────────

def adapter_cv_avec_gemini(cv_html: str, offre: dict, llm: ChatGoogleGenerativeAI) -> str:
    """
    Appelle Gemini en streaming via LangChain pour générer le CV HTML adapté.
    Retourne le HTML complet prêt à être sauvegardé.
    """
    prompt = construire_prompt(cv_html, offre)
    cv_adapte = ""
    messages = [
        SystemMessage(content=SYSTEM_PROMPT_CV),
        HumanMessage(content=prompt),
    ]
    for chunk in llm.stream(messages):
        if chunk.content:
            cv_adapte += chunk.content
    return cv_adapte.strip()


def nettoyer_html(html: str) -> str:
    """
    Nettoie la réponse Gemini : supprime d'éventuels blocs markdown
    (```html ... ```) si Gemini les a ajoutés malgré les instructions.
    """
    # Supprimer les blocs ```html ... ```
    html = re.sub(r"^```(?:html)?\s*\n?", "", html.strip())
    html = re.sub(r"\n?```\s*$", "", html.strip())
    return html.strip()


# ─────────────────────────────────────────────
# Point d'entrée principal
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Adapte votre CV HTML pour une offre d'emploi spécifique"
    )
    parser.add_argument(
        "--offre-id",
        required=True,
        help="Identifiant de l'offre (visible dans le dashboard ou le rapport .txt)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Chemin de sortie du .html (défaut : cv/cv_adapte_<id>.html)",
    )
    args = parser.parse_args()

    console.print("\n[bold cyan]Agent de Recherche d'Emploi — Adaptation CV[/bold cyan]")
    console.print("━" * 50)

    # Vérifier la clé API
    api_key = os.getenv("GOOGLE_AI_STUDIO_KEY")
    if not api_key:
        console.print("[red]Erreur :[/red] GOOGLE_AI_STUDIO_KEY manquante dans .env")
        raise SystemExit(1)

    llm = ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite-preview",
        google_api_key=api_key,
        temperature=0.2,
        max_output_tokens=8192,
    )

    db_path = os.getenv("DB_PATH", "data/offers.db")
    cv_path = os.getenv("CV_PATH", "cv/cv_base.html")
    output_dir = os.getenv("OUTPUT_DIR", "cv/")

    # Chemin de sortie du HTML adapté
    offre_id_court = args.offre_id.replace("adzuna_", "")[:12]
    chemin_sortie = args.output or os.path.join(
        output_dir, f"cv_adapte_{offre_id_court}.html"
    )

    init_db(db_path)

    # Récupérer l'offre depuis la DB
    with get_connection(db_path) as conn:
        offre = conn.execute(
            "SELECT * FROM offres WHERE id = ?", (args.offre_id,)
        ).fetchone()

    if not offre:
        console.print(f"[red]Offre introuvable :[/red] {args.offre_id}")
        console.print("[dim]Vérifiez l'ID avec : python src/dashboard.py[/dim]")
        raise SystemExit(1)

    offre_dict = dict(offre)

    # Afficher l'offre cible
    console.print(Panel(
        f"[bold]{offre_dict['intitule']}[/bold]\n"
        f"[cyan]{offre_dict['entreprise_nom'] or 'Entreprise non précisée'}[/cyan]"
        f" — {offre_dict['lieu_travail'] or ''}\n"
        f"Score : [green]{offre_dict['score']}[/green]/100\n"
        f"[dim]{offre_dict['url'] or ''}[/dim]",
        title="Offre cible",
        border_style="cyan",
    ))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        # Étape 1 : Lecture du CV HTML
        tache = progress.add_task("Lecture du CV HTML...", total=None)
        cv_html = lire_cv_html(cv_path)
        nb_lignes = len(cv_html.split("\n"))
        progress.update(tache, description=f"[green]CV lu ({nb_lignes} lignes)[/green]")

        # Étape 2 : Adaptation avec Gemini (streaming)
        progress.update(tache, description="Adaptation avec Gemini...")
        cv_adapte = adapter_cv_avec_gemini(cv_html, offre_dict, llm)
        cv_adapte = nettoyer_html(cv_adapte)
        progress.update(tache, description="[green]CV adapté généré[/green]")

        # Étape 3 : Sauvegarde du HTML
        progress.update(tache, description="Sauvegarde du fichier HTML...")
        os.makedirs(os.path.dirname(chemin_sortie) or ".", exist_ok=True)
        with open(chemin_sortie, "w", encoding="utf-8") as f:
            f.write(cv_adapte)
        progress.update(tache, description="[green]Fichier HTML sauvegardé[/green]")

    # Résumé
    console.print()
    console.print(f"[green]✓[/green] CV adapté généré avec succès")
    console.print(f"[cyan]→[/cyan] {os.path.abspath(chemin_sortie)}")
    console.print()
    console.print("[dim]Pour exporter en PDF : ouvrir dans Chrome → Imprimer → Enregistrer en PDF[/dim]")
    console.print("[dim](Le CSS @media print formate automatiquement en A4 une page)[/dim]")


if __name__ == "__main__":
    main()
