"""
Module de gestion de la base de données SQLite.
Partagé par tous les scripts — centralise le schéma et les connexions.
"""

import os
import sqlite3


# Chemin par défaut (surchargeable via variable d'environnement)
DB_PATH_DEFAUT = os.getenv("DB_PATH", "data/offers.db")


def get_connection(db_path: str = DB_PATH_DEFAUT) -> sqlite3.Connection:
    """
    Retourne une connexion SQLite avec row_factory activé.
    row_factory permet d'accéder aux colonnes par nom (offre['intitule']).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DB_PATH_DEFAUT) -> None:
    """
    Initialise la base de données : crée le dossier, la table et l'index
    si ils n'existent pas encore. Idempotent (sûr d'appeler plusieurs fois).
    """
    # Créer le dossier data/ si nécessaire
    dossier = os.path.dirname(db_path)
    if dossier:
        os.makedirs(dossier, exist_ok=True)

    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offres (
                id                TEXT PRIMARY KEY,
                intitule          TEXT,
                description       TEXT,
                entreprise_nom    TEXT,
                lieu_travail      TEXT,
                type_contrat      TEXT,
                salaire_libelle   TEXT,
                date_creation     TEXT,
                url               TEXT,
                raw_json          TEXT,
                score             INTEGER,
                score_explication TEXT,
                score_points_forts TEXT,
                score_points_faibles TEXT,
                score_date        TEXT,
                collected_at      TEXT DEFAULT (datetime('now'))
            )
        """)

        # Index pour accélérer le tri par score dans le dashboard
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_score ON offres(score DESC)"
        )

        conn.commit()


if __name__ == "__main__":
    # Test rapide : initialiser la DB et afficher le chemin
    chemin = DB_PATH_DEFAUT
    init_db(chemin)
    print(f"Base de données initialisée : {os.path.abspath(chemin)}")

    with get_connection(chemin) as conn:
        curseur = conn.execute("SELECT COUNT(*) as total FROM offres")
        total = curseur.fetchone()["total"]
        print(f"Nombre d'offres en base : {total}")
