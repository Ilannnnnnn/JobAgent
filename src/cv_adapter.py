"""
Adaptateur de CV HTML pour une offre d'emploi spécifique.

Utilisation :
    python src/cv_adapter.py --offre-id <ID_OFFRE>
    python src/cv_adapter.py --offre-id <ID> --output cv/mon_cv_adapte.html

Ce script :
1. Lit l'offre ciblée depuis la base SQLite
2. Lit le CV de base au format HTML
3. Charge le portfolio HTML local pour enrichir le contexte
4. Demande à Claude Sonnet d'adapter le contenu pour cette offre (streaming)
5. Génère un fichier HTML adapté — ouvrir dans le navigateur et imprimer en A4
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import argparse
import logging
import os
import sys
from pathlib import Path

import anthropic
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

# Ajouter le dossier src/ au path pour importer db.py
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection
from scorer import formater_profil

load_dotenv()

console = Console(legacy_windows=False)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ─────────────────────────────────────────────
# Chargement du portfolio local
# ─────────────────────────────────────────────

def charger_portfolio(portfolio_path: str) -> str:
    if not portfolio_path or not Path(portfolio_path).exists():
        logging.warning("PORTFOLIO_PATH absent ou invalide — portfolio ignoré")
        return ""
    textes = []
    for html_file in sorted(Path(portfolio_path).rglob("*.html")):
        try:
            soup = BeautifulSoup(
                html_file.read_text(encoding="utf-8", errors="ignore"),
                "html.parser"
            )
            for tag in soup(["script", "style", "nav", "footer", "head"]):
                tag.decompose()
            texte = soup.get_text(separator=" ", strip=True)
            if len(texte) > 100:
                textes.append(f"=== {html_file.name} ===\n{texte[:8000]}")
        except Exception as e:
            logging.warning("Erreur lecture portfolio %s : %s", html_file, e)
    logging.info("Portfolio chargé : %d pages HTML", len(textes))
    return "\n\n".join(textes)


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
    console.print("-" * 50)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]Erreur :[/red] ANTHROPIC_API_KEY manquante dans .env")
        raise SystemExit(1)

    db_path = os.getenv("DB_PATH", "data/offers.db")
    cv_path = os.getenv("CV_PATH", "cv/cv_base.html")
    output_dir = os.getenv("OUTPUT_DIR", "cv/")
    portfolio_path = os.getenv("PORTFOLIO_PATH", "")
    profile_path = os.getenv("PROFILE_PATH", "config/profile.yaml")

    offre_id_court = args.offre_id.replace("adzuna_", "")[:12]
    chemin_sortie = args.output or os.path.join(
        output_dir, f"cv_adapte_{offre_id_court}.html"
    )

    init_db(db_path)

    with get_connection(db_path) as conn:
        offre = conn.execute(
            "SELECT * FROM offres WHERE id = ?", (args.offre_id,)
        ).fetchone()

    if not offre:
        console.print(f"[red]Offre introuvable :[/red] {args.offre_id}")
        console.print("[dim]Vérifiez l'ID avec : python src/dashboard.py[/dim]")
        raise SystemExit(1)

    offre_dict = dict(offre)

    console.print(Panel(
        f"[bold]{offre_dict['intitule']}[/bold]\n"
        f"[cyan]{offre_dict['entreprise_nom'] or 'Entreprise non précisée'}[/cyan]"
        f" — {offre_dict['lieu_travail'] or ''}\n"
        f"Score : [green]{offre_dict['score']}[/green]/100\n"
        f"[dim]{offre_dict['url'] or ''}[/dim]",
        title="Offre cible",
        border_style="cyan",
    ))

    # Chargement CV + profil + portfolio
    console.print("[dim]Lecture du CV HTML...[/dim]")
    cv_base_html = lire_cv_html(cv_path)

    console.print("[dim]Chargement du profil YAML...[/dim]")
    if os.path.exists(profile_path):
        with open(profile_path, encoding="utf-8") as f:
            profil_yaml = yaml.safe_load(f)
        profil_texte = formater_profil(profil_yaml)
    else:
        profil_texte = ""

    console.print("[dim]Chargement du portfolio...[/dim]")
    contenu_portfolio = charger_portfolio(portfolio_path)

    # Supprime le CSS du CV pour alléger le prompt — Claude n'a pas besoin du CSS pour adapter le contenu
    soup_cv = BeautifulSoup(cv_base_html, "html.parser")
    for tag in soup_cv.find_all("style"):
        tag.decompose()
    cv_base_sans_css = str(soup_cv)

    contexte_candidat = f"""
== PROFIL YAML ==
{profil_texte}

== PORTFOLIO & EXPÉRIENCES DÉTAILLÉES ==
{contenu_portfolio if contenu_portfolio else "Non disponible — utilise uniquement le profil YAML"}
"""

    offre = offre_dict  # alias pour la construction du prompt
    prompt = f"""Tu es un expert en rédaction de CV technique pour des postes en ingénierie IA et LLM. Adapte le CV HTML ci-dessous pour maximiser sa pertinence pour l'offre d'emploi fournie.

== OFFRE D'EMPLOI ==
Poste : {offre['intitule']}
Entreprise : {offre['entreprise_nom']}
Description : {offre['description'][:3000]}
Analyse de l'offre : {offre.get('score_explication', '')}
Points forts du profil pour ce poste : {offre.get('score_points_forts', '')}
Points faibles à compenser : {offre.get('score_points_faibles', '')}

== PROFIL YAML & CRITÈRES ==
{profil_texte}

== PORTFOLIO & EXPÉRIENCES DÉTAILLÉES ==
{contenu_portfolio if contenu_portfolio else "Non disponible"}
Utilise ces informations pour enrichir les descriptions d'expériences et de projets avec des détails concrets, des chiffres et des résultats réels issus du portfolio. Ne fabrique rien qui n'y soit pas mentionné.

== CV HTML DE BASE ==
{cv_base_sans_css}

Instructions STRICTES — à respecter absolument :
- Le CV DOIT tenir sur UNE SEULE PAGE A4 (297mm × 210mm, padding 32px 42px)
- L'accroche doit faire 2/3 phrases maximum, 40 mots maximum
- Pour les expériences : maximum 3 bullet points par expérience, chaque bullet maximum 1 ligne (≈ 90 caractères)
- Pour les projets : maximum 1 bullet point par projet, 1 ligne maximum
- Si nécessaire, supprime les projets les moins pertinents pour l'offre — garde maximum 2 projets
- Formation : garde uniquement le diplôme principal et les certifications clés (Voltaire, TOEIC)
- Compétences : garde maximum 4 catégories, maximum 6 items par catégorie
- Si besoin, supprime la section "Formation Agents IA · Orange Innovation" — c'est redondant avec l'expérience
- Conserve la structure HTML et le style CSS intégralement
- Reformule l'accroche pour correspondre exactement au poste et à l'entreprise en 2 phrases max
- Réordonne et reformule les compétences pour mettre en avant celles demandées par l'offre
- Adapte les descriptions d'expériences avec le vocabulaire de l'offre
- Ne fabrique aucune information absente du profil, du CV de base ou du portfolio
- Retourne uniquement le HTML complet, sans commentaires ni backticks
- Utilise tout l'espace disponible sur la page — si de l'espace reste en bas, enrichis les bullet points existants avec des détails supplémentaires pertinents pour l'offre plutôt que de laisser du blanc
- Pour l'accroche : mentionne explicitement le nom de l'entreprise cible et le domaine spécifique du poste dans la première phrase
- Renomme "Développeur Auto-Entrepreneur · Cycle Produit Complet" en "Auto-Entrepreneur · Développement Produit IA & Web"
"""

    console.print("\n[bold cyan]Adaptation avec Claude Sonnet (streaming)...[/bold cyan]\n")

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        cv_adapte = ""
        for text in stream.text_stream:
            print(text, end="", flush=True)
            cv_adapte += text

    if "<" in cv_adapte:
        cv_adapte = cv_adapte[cv_adapte.index("<"):]
    cv_adapte = cv_adapte.strip().strip("```html").strip("```").strip()

    # Réinjecte le CSS original dans le HTML généré par Claude
    soup_original = BeautifulSoup(cv_base_html, "html.parser")
    style_original = soup_original.find("style")
    soup_adapte = BeautifulSoup(cv_adapte, "html.parser")
    head = soup_adapte.find("head")
    if head and style_original:
        for s in soup_adapte.find_all("style"):
            s.decompose()
        head.insert(0, style_original)
        cv_adapte = str(soup_adapte)

    console.print("\n")
    os.makedirs(os.path.dirname(chemin_sortie) or ".", exist_ok=True)
    with open(chemin_sortie, "w", encoding="utf-8") as f:
        f.write(cv_adapte)

    console.print(f"[green]✓[/green] CV adapté généré avec succès")
    console.print(f"[cyan]→[/cyan] {os.path.abspath(chemin_sortie)}")
    console.print()
    console.print("[dim]Pour exporter en PDF : ouvrir dans Chrome → Imprimer → Enregistrer en PDF[/dim]")
    console.print("[dim](Le CSS @media print formate automatiquement en A4 une page)[/dim]")


if __name__ == "__main__":
    main()
