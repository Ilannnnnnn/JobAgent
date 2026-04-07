"""
Collecteur d'offres d'emploi — API Adzuna.

Utilisation :
    python src/collector.py

Ce script :
1. Boucle sur toutes les cibles (pays + ville) définies dans sources.yaml
2. Filtre les deal-breakers avant insertion
3. Sauvegarde les nouvelles offres dans SQLite (doublons ignorés)

Inscription Adzuna : https://developer.adzuna.com
Codes pays supportés : fr, gb, us, de, nl, be, ch, es, it, au, ca
"""

import json
import os
import sys
import time

import requests
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

# Ajouter le dossier src/ au path pour importer db.py
sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_connection

load_dotenv()

console = Console()


# ─────────────────────────────────────────────
# Chargement de la configuration
# ─────────────────────────────────────────────

def charger_config() -> tuple[dict, dict]:
    """Charge profile.yaml et sources.yaml."""
    profil_path = os.getenv("PROFILE_PATH", "config/profile.yaml")
    sources_path = os.getenv("SOURCES_PATH", "config/sources.yaml")

    with open(profil_path, encoding="utf-8") as f:
        profil = yaml.safe_load(f)

    with open(sources_path, encoding="utf-8") as f:
        sources = yaml.safe_load(f)

    return profil, sources


# ─────────────────────────────────────────────
# Appel API Adzuna pour une cible (pays + ville)
# ─────────────────────────────────────────────

def rechercher_offres_cible(
    app_id: str,
    app_key: str,
    mots_cles: str,
    pays: str,
    localisation: str,
    max_results: int,
    criteres: dict,
    config: dict,
) -> list[dict]:
    """
    Appelle l'API Adzuna pour une cible donnée (pays + localisation).
    Gère la pagination jusqu'à max_results offres.
    """
    par_page = min(50, max_results)
    toutes_offres = []
    page = 1

    while len(toutes_offres) < max_results:
        url = f"{config['base_url']}/v1/api/jobs/{pays}/search/{page}"

        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": mots_cles,
            "where": localisation,
            "results_per_page": par_page,
        }

        if criteres.get("distance_km"):
            params["distance"] = criteres["distance_km"]

        if criteres.get("salaire_min_annuel"):
            params["salary_min"] = criteres["salaire_min_annuel"]

        reponse = requests.get(
            url,
            params=params,
            timeout=config.get("timeout_seconds", 30),
        )

        if reponse.status_code != 200:
            console.print(
                f"  [red]Erreur ({reponse.status_code})[/red] "
                f"{pays.upper()} / {localisation} : {reponse.text[:150]}"
            )
            break

        donnees = reponse.json()
        resultats = donnees.get("results", [])

        if not resultats:
            break

        toutes_offres.extend(resultats)

        if len(resultats) < par_page:
            break

        page += 1
        time.sleep(1)

    return toutes_offres[:max_results]


# ─────────────────────────────────────────────
# Filtrage des deal-breakers
# ─────────────────────────────────────────────

def contient_deal_breaker(offre: dict, deal_breakers: list[str]) -> bool:
    """Vérifie si une offre contient un deal-breaker (insensible à la casse)."""
    texte = " ".join([
        offre.get("title", ""),
        offre.get("description", ""),
    ]).lower()

    return any(db.lower() in texte for db in deal_breakers)


# ─────────────────────────────────────────────
# Normalisation Adzuna → format interne
# ─────────────────────────────────────────────

def normaliser_offre(offre_brute: dict, pays: str) -> dict:
    """
    Transforme une offre brute Adzuna en format normalisé pour SQLite.
    Le pays est inclus dans l'ID pour éviter les collisions entre sources.
    """
    sal_min = offre_brute.get("salary_min")
    sal_max = offre_brute.get("salary_max")
    if sal_min and sal_max:
        salaire_libelle = f"{int(sal_min):,} – {int(sal_max):,}".replace(",", " ")
        salaire_libelle += " £/an" if pays == "gb" else " €/an"
    elif sal_min:
        salaire_libelle = f"À partir de {int(sal_min):,}".replace(",", " ")
        salaire_libelle += " £/an" if pays == "gb" else " €/an"
    else:
        salaire_libelle = ""

    contract_time = offre_brute.get("contract_time", "")
    contract_type = offre_brute.get("contract_type", "")
    type_contrat = ""
    if contract_time == "full_time":
        type_contrat = "Temps plein"
    elif contract_time == "part_time":
        type_contrat = "Temps partiel"
    if contract_type == "permanent":
        type_contrat = "CDI" if pays == "fr" else "Permanent"
    elif contract_type == "contract":
        type_contrat = "CDD/Contrat"

    return {
        "id": f"adzuna_{pays}_{offre_brute.get('id', '')}",
        "intitule": offre_brute.get("title", ""),
        "description": offre_brute.get("description", ""),
        "entreprise_nom": offre_brute.get("company", {}).get("display_name", ""),
        "lieu_travail": offre_brute.get("location", {}).get("display_name", ""),
        "type_contrat": type_contrat,
        "salaire_libelle": salaire_libelle,
        "date_creation": offre_brute.get("created", ""),
        "url": offre_brute.get("redirect_url", ""),
        "raw_json": json.dumps(offre_brute, ensure_ascii=False),
    }


# ─────────────────────────────────────────────
# Sauvegarde en base de données
# ─────────────────────────────────────────────

def sauvegarder_offres(offres_normalisees: list[dict], db_path: str) -> int:
    """Insère les offres dans SQLite. Doublons ignorés. Retourne le nombre de nouvelles."""
    nouvelles = 0

    with get_connection(db_path) as conn:
        for offre in offres_normalisees:
            curseur = conn.execute(
                """
                INSERT OR IGNORE INTO offres
                    (id, intitule, description, entreprise_nom, lieu_travail,
                     type_contrat, salaire_libelle, date_creation, url, raw_json)
                VALUES
                    (:id, :intitule, :description, :entreprise_nom, :lieu_travail,
                     :type_contrat, :salaire_libelle, :date_creation, :url, :raw_json)
                """,
                offre,
            )
            if curseur.rowcount > 0:
                nouvelles += 1

        conn.commit()

    return nouvelles


# ─────────────────────────────────────────────
# Point d'entrée principal
# ─────────────────────────────────────────────

def main():
    console.print("\n[bold cyan]Agent de Recherche d'Emploi — Collecte (Adzuna)[/bold cyan]")
    console.print("━" * 50)

    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")

    if not app_id or not app_key:
        console.print("[red]Erreur :[/red] ADZUNA_APP_ID et ADZUNA_APP_KEY manquants dans .env")
        raise SystemExit(1)

    profil, sources = charger_config()
    config_adzuna = sources["adzuna"]
    deal_breakers = profil.get("deal_breakers", [])
    criteres = profil.get("criteres", {})
    mots_cles = profil["candidat"]["poste_cible"]
    db_path = os.getenv("DB_PATH", "data/offers.db")
    max_par_cible = config_adzuna.get("search_params", {}).get("max_results_par_cible", 25)

    cibles = config_adzuna.get("cibles", [])
    if not cibles:
        console.print("[red]Erreur :[/red] Aucune cible définie dans config/sources.yaml")
        raise SystemExit(1)

    init_db(db_path)

    total_brutes = 0
    total_filtrees = 0
    total_nouvelles = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        for cible in cibles:
            pays = cible["pays"]
            localisation = cible["localisation"]
            label = f"[bold]{pays.upper()}[/bold] / {localisation}"

            # mots_cles peut être une string ou une liste
            cle_brute = cible.get("mots_cles") or mots_cles
            liste_mots_cles = [cle_brute] if isinstance(cle_brute, str) else cle_brute

            tache = progress.add_task(
                f"Recherche {label} — [italic]{', '.join(liste_mots_cles)}[/italic]...",
                total=None,
            )

            # Recherche pour chaque mot-clé, déduplication par ID Adzuna brut
            offres_par_id: dict[str, dict] = {}
            for mot_cle in liste_mots_cles:
                resultats = rechercher_offres_cible(
                    app_id, app_key, mot_cle, pays, localisation,
                    max_par_cible, criteres, config_adzuna,
                )
                for offre in resultats:
                    offres_par_id[offre.get("id", "")] = offre
                time.sleep(1)

            offres_brutes = list(offres_par_id.values())
            total_brutes += len(offres_brutes)

            # Filtrage deal-breakers
            filtrees = 0
            offres_normalisees = []
            for offre in offres_brutes:
                if contient_deal_breaker(offre, deal_breakers):
                    filtrees += 1
                else:
                    offres_normalisees.append(normaliser_offre(offre, pays))
            total_filtrees += filtrees

            # Sauvegarde
            nouvelles = sauvegarder_offres(offres_normalisees, db_path)
            total_nouvelles += nouvelles

            progress.update(
                tache,
                description=f"[green]✓[/green] {label} — "
                            f"{len(offres_brutes)} trouvées, "
                            f"{filtrees} filtrées, "
                            f"[cyan]{nouvelles} nouvelles[/cyan]",
            )

            time.sleep(1)

    # Résumé global
    console.print()
    console.print(f"[bold]Résumé ({len(cibles)} cibles)[/bold]")
    console.print(f"[green]✓[/green] {total_brutes} offres récupérées au total")
    console.print(f"[yellow]✗[/yellow] {total_filtrees} offres filtrées (deal-breakers)")
    console.print(f"[cyan]→[/cyan] {total_nouvelles} nouvelles offres ajoutées en base")
    console.print()
    console.print("[dim]Prochaine étape : python src/scorer.py[/dim]")


if __name__ == "__main__":
    main()
