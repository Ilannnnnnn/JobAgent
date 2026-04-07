"""
Dashboard CLI — Affichage des offres scorées.

Utilisation :
    python src/dashboard.py                   # Top 10 offres + export auto
    python src/dashboard.py --top 20          # Top 20 offres
    python src/dashboard.py --detail <ID>     # Détail d'une offre
    python src/dashboard.py --toutes          # Toutes les offres
    python src/dashboard.py --no-export       # Affichage sans générer le fichier texte
"""

import argparse
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Ajouter le dossier src/ au path pour importer db.py
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection

load_dotenv()

console = Console()


# ─────────────────────────────────────────────
# Couleur selon le score
# ─────────────────────────────────────────────

def couleur_score(score: int) -> str:
    if score >= 80:
        return "bold green"
    elif score >= 60:
        return "yellow"
    elif score >= 40:
        return "orange3"
    else:
        return "red"


# ─────────────────────────────────────────────
# Affichage terminal (tableau compact)
# ─────────────────────────────────────────────

def afficher_tableau(offres: list, titre: str = "Top Offres d'Emploi") -> None:
    """Affiche les offres dans un tableau rich — une ligne par offre."""
    if not offres:
        console.print("\n[yellow]Aucune offre scorée trouvée.[/yellow]")
        console.print("[dim]Lancez d'abord : python src/collector.py && python src/scorer.py[/dim]\n")
        return

    table = Table(
        title=titre,
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        show_lines=True,
        expand=True,
    )

    table.add_column("#", style="dim", width=3, justify="right", no_wrap=True)
    table.add_column("Score", width=6, justify="center", no_wrap=True)
    table.add_column("Titre du poste", style="bold", min_width=25, ratio=3)
    table.add_column("Entreprise", min_width=15, ratio=2)
    table.add_column("Lieu", min_width=12, ratio=2)
    table.add_column("Contrat", min_width=8, ratio=1)
    table.add_column("Salaire", min_width=15, ratio=2)

    for rang, offre in enumerate(offres, 1):
        score = offre["score"]
        couleur = couleur_score(score)

        table.add_row(
            str(rang),
            f"[{couleur}]{score}[/{couleur}]",
            offre["intitule"] or "",
            offre["entreprise_nom"] or "N/A",
            offre["lieu_travail"] or "",
            offre["type_contrat"] or "?",
            offre["salaire_libelle"] or "Non précisé",
        )

    console.print()
    console.print(table)


# ─────────────────────────────────────────────
# Export fichier texte
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

        # Indicateur visuel du score
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

        # Analyse Gemini
        if offre.get("score_explication"):
            lignes += [
                "  ANALYSE :",
                f"  {offre['score_explication']}",
                "",
            ]

        # Points forts
        try:
            points_forts = json.loads(offre.get("score_points_forts") or "[]")
            if points_forts:
                lignes.append("  POINTS FORTS :")
                for p in points_forts:
                    lignes.append(f"    + {p}")
                lignes.append("")
        except (json.JSONDecodeError, TypeError):
            pass

        # Points faibles
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
# Affichage détail d'une offre
# ─────────────────────────────────────────────

def afficher_detail(offre_id: str, db_path: str) -> None:
    """Affiche le détail complet d'une offre."""
    with get_connection(db_path) as conn:
        offre = conn.execute(
            "SELECT * FROM offres WHERE id = ?", (offre_id,)
        ).fetchone()

    if not offre:
        console.print(f"\n[red]Offre introuvable :[/red] {offre_id}\n")
        return

    offre = dict(offre)

    console.print()
    console.print(Panel(
        f"[bold]{offre['intitule'] or 'Titre non précisé'}[/bold]\n"
        f"[cyan]{offre['entreprise_nom'] or 'N/A'}[/cyan]  •  "
        f"{offre['lieu_travail'] or ''}  •  "
        f"{offre['type_contrat'] or ''}  •  "
        f"{offre['salaire_libelle'] or 'Salaire non précisé'}\n"
        f"[link]{offre['url'] or ''}[/link]",
        title=f"[bold]Offre {offre['id']}[/bold]",
        border_style="cyan",
    ))

    if offre["score"] is not None and offre["score"] >= 0:
        score = offre["score"]
        couleur = couleur_score(score)

        points_forts = []
        points_faibles = []
        try:
            points_forts = json.loads(offre.get("score_points_forts") or "[]")
            points_faibles = json.loads(offre.get("score_points_faibles") or "[]")
        except (json.JSONDecodeError, TypeError):
            pass

        forts = "\n".join(f"  [green]✓[/green] {p}" for p in points_forts) or "  [dim]N/A[/dim]"
        faibles = "\n".join(f"  [red]✗[/red] {p}" for p in points_faibles) or "  [dim]N/A[/dim]"

        console.print(Panel(
            f"[{couleur}]Score : {score}/100[/{couleur}]\n\n"
            f"[bold]Analyse :[/bold]\n{offre['score_explication'] or 'N/A'}\n\n"
            f"[bold]Points forts :[/bold]\n{forts}\n\n"
            f"[bold]Points faibles :[/bold]\n{faibles}",
            title="Évaluation Gemini",
            border_style=couleur.replace("bold ", ""),
        ))

    description = (offre["description"] or "Aucune description")[:1500]
    console.print(Panel(description, title="Description", border_style="dim"))
    console.print()


# ─────────────────────────────────────────────
# Statistiques globales
# ─────────────────────────────────────────────

def afficher_stats(db_path: str) -> None:
    with get_connection(db_path) as conn:
        stats = conn.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(CASE WHEN score IS NOT NULL AND score >= 0 THEN 1 END) as scorees,
                COUNT(CASE WHEN score IS NULL THEN 1 END) as non_scorees,
                ROUND(AVG(CASE WHEN score >= 0 THEN score END), 1) as score_moyen,
                MAX(CASE WHEN score >= 0 THEN score END) as score_max
            FROM offres
        """).fetchone()

    if not stats or stats["total"] == 0:
        console.print("[dim]Base de données vide[/dim]")
        return

    stats = dict(stats)
    panneau = (
        f"Total : [bold]{stats['total']}[/bold] offres  "
        f"| Scorées : [green]{stats['scorees']}[/green]  "
        f"| En attente : [yellow]{stats['non_scorees']}[/yellow]  "
        f"| Score moyen : [cyan]{stats['score_moyen'] or 'N/A'}[/cyan]  "
        f"| Meilleur : [bold green]{stats['score_max'] or 'N/A'}[/bold green]"
    )
    console.print(Panel(panneau, title="Statistiques", border_style="dim"))


# ─────────────────────────────────────────────
# Point d'entrée principal
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dashboard des offres d'emploi scorées")
    parser.add_argument("--top", type=int, default=10, help="Nombre d'offres (défaut : 10)")
    parser.add_argument("--detail", metavar="ID", help="Détail d'une offre par son ID")
    parser.add_argument("--toutes", action="store_true", help="Inclure les offres non scorées")
    parser.add_argument("--no-export", action="store_true", help="Ne pas générer le fichier texte")
    args = parser.parse_args()

    db_path = os.getenv("DB_PATH", "data/offers.db")
    init_db(db_path)

    console.print("\n[bold cyan]Agent de Recherche d'Emploi — Dashboard[/bold cyan]")
    console.print("━" * 50)

    if args.detail:
        afficher_detail(args.detail, db_path)
        return

    afficher_stats(db_path)

    with get_connection(db_path) as conn:
        if args.toutes:
            offres = conn.execute(
                "SELECT * FROM offres ORDER BY COALESCE(score, -999) DESC LIMIT ?",
                (args.top,),
            ).fetchall()
            titre = f"Top {args.top} Offres (toutes)"
        else:
            offres = conn.execute(
                "SELECT * FROM offres WHERE score IS NOT NULL AND score >= 0 ORDER BY score DESC LIMIT ?",
                (args.top,),
            ).fetchall()
            titre = f"Top {min(args.top, len(offres))} Offres Scorées"

    offres = [dict(o) for o in offres]
    afficher_tableau(offres, titre=titre)

    if not offres:
        return

    # Export fichier texte : toutes les offres scorées (pas seulement le top N affiché)
    if not args.no_export:
        with get_connection(db_path) as conn:
            toutes_scorees = conn.execute(
                "SELECT * FROM offres WHERE score IS NOT NULL AND score >= 0 ORDER BY score DESC"
            ).fetchall()
        toutes_scorees = [dict(o) for o in toutes_scorees]

        horodatage = datetime.now().strftime("%Y%m%d_%H%M")
        chemin_export = f"data/offres_{horodatage}.txt"
        exporter_txt(toutes_scorees, chemin_export)
        console.print(f"[green]✓[/green] Rapport exporté ({len(toutes_scorees)} offres) : [bold]{chemin_export}[/bold]")

    console.print("[dim]Voir le détail : python src/dashboard.py --detail <ID>[/dim]")
    console.print()


if __name__ == "__main__":
    main()
