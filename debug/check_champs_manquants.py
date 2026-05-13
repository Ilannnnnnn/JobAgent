import sqlite3, os
from dotenv import load_dotenv
load_dotenv(override=True)

conn = sqlite3.connect(os.getenv("DB_PATH", "data/offers.db"))
conn.row_factory = sqlite3.Row

print("=== Offres Indeed avec champs manquants ===")
rows = conn.execute("""
    SELECT id, intitule, entreprise_nom, type_contrat, salaire_libelle, url
    FROM offres
    WHERE source = 'indeed'
    AND (type_contrat IS NULL OR type_contrat = '' 
         OR salaire_libelle IS NULL OR salaire_libelle = '')
    LIMIT 10
""").fetchall()

print(f"Indeed sans contrat ou salaire : {len(rows)}")
for r in rows:
    print(f"\n  {r['intitule']} | {r['entreprise_nom']}")
    print(f"  Contrat: {repr(r['type_contrat'])} | Salaire: {repr(r['salaire_libelle'])}")
    print(f"  URL: {r['url'][:80] if r['url'] else 'N/A'}")

print("\n=== Stats champs vides par source ===")
for source in ['indeed', 'adzuna', 'apec', 'linkedin', 'wttj']:
    row = conn.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN type_contrat IS NULL OR type_contrat = '' THEN 1 ELSE 0 END) as sans_contrat,
            SUM(CASE WHEN salaire_libelle IS NULL OR salaire_libelle = '' THEN 1 ELSE 0 END) as sans_salaire
        FROM offres WHERE source = ?
    """, (source,)).fetchone()
    print(f"  {source:10} | total={row['total']:3} | sans_contrat={row['sans_contrat']:3} | sans_salaire={row['sans_salaire']:3}")
