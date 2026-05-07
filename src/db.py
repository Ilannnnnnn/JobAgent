"""
Module de gestion de la base de données SQLite.
Partagé par tous les scripts — centralise le schéma et les connexions.
"""

import logging
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

        # Contrainte d'unicité sur l'URL (prévient les doublons cross-sources)
        # On déduplique d'abord les lignes existantes (garde le rowid le plus bas)
        # pour que la création de l'index unique ne lève pas IntegrityError.
        conn.execute("""
            DELETE FROM offres
            WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM offres GROUP BY url
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_url ON offres(url)"
        )

        # Ajout de la colonne statut (ignoré si elle existe déjà)
        try:
            conn.execute(
                "ALTER TABLE offres ADD COLUMN statut TEXT DEFAULT 'À postuler'"
            )
        except sqlite3.OperationalError:
            pass  # Colonne déjà présente

        # Ajout de la colonne source (ignoré si elle existe déjà)
        try:
            conn.execute("ALTER TABLE offres ADD COLUMN source TEXT")
        except sqlite3.OperationalError:
            pass  # Colonne déjà présente

        # Ajout de la colonne date_postulation (ignoré si elle existe déjà)
        try:
            conn.execute("ALTER TABLE offres ADD COLUMN date_postulation TEXT")
        except sqlite3.OperationalError:
            pass  # Colonne déjà présente

        # Migration : déduit la source depuis l'id pour les lignes sans valeur
        conn.execute("""
            UPDATE offres SET source =
            CASE
                WHEN id LIKE 'apec%'    THEN 'apec'
                WHEN id LIKE 'indeed%'  THEN 'indeed'
                WHEN id LIKE 'adzuna%'  THEN 'adzuna'
                WHEN id LIKE 'wttj%'    THEN 'wttj'
                WHEN id LIKE 'google%'   THEN 'google'
                WHEN id LIKE 'linkedin%' THEN 'linkedin'
                WHEN id LIKE 'manuel%'  THEN 'manuel'
                ELSE 'inconnu'
            END
            WHERE source IS NULL
        """)

        # Migration : remplace les codes numériques APEC par des libellés lisibles
        conn.execute("""
            UPDATE offres SET type_contrat = CASE type_contrat
                WHEN '101887' THEN 'CDD'
                WHEN '101888' THEN 'CDI'
                WHEN '101889' THEN 'Interim'
                WHEN '101906' THEN 'Alternance'
                WHEN '597137' THEN 'CDI'
                ELSE type_contrat
            END
            WHERE source = 'apec' AND type_contrat GLOB '[0-9]*'
        """)

        # Migration one-shot : supprime les offres APEC avec des URLs invalides
        # (contenant typesConvention) pour forcer un re-scraping propre.
        # Sans effet une fois les offres purgées.
        conn.execute("""
            DELETE FROM offres
            WHERE source = 'apec' AND url LIKE '%typesConvention%'
        """)

        # Migration : insère le segment detail-offre/ manquant dans les URLs APEC
        try:
            conn.execute("""
                UPDATE offres
                SET url = REPLACE(url,
                    '/emploi/' || SUBSTR(url, INSTR(url, '/emploi/') + 8, 20),
                    '/emploi/detail-offre/' || SUBSTR(url, INSTR(url, '/emploi/') + 8, 20)
                )
                WHERE source = 'apec'
                AND url NOT LIKE '%/detail-offre/%'
            """)
        except Exception as e:
            logging.warning("Migration URL APEC : %s", e)

        # Nettoie les intitulés LinkedIn qui contiennent une URL au lieu d'un titre
        conn.execute("""
            UPDATE offres
            SET intitule = ''
            WHERE source = 'linkedin'
            AND (intitule LIKE 'http%' OR intitule LIKE 'www%')
        """)

        # Supprime les offres LinkedIn vides (titre absent et score nul)
        conn.execute("""
            DELETE FROM offres
            WHERE source = 'linkedin'
            AND (intitule = '' OR intitule IS NULL)
            AND score = 0
        """)

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
