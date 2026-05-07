import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()
db_path = os.getenv("DB_PATH", "data/offers.db")

conn = sqlite3.connect(db_path)

print("=== 5 dernières offres LinkedIn modifiées ===")
for row in conn.execute("""
    SELECT id, intitule, entreprise_nom, source, score, 
           LENGTH(description) as desc_len,
           collected_at
    FROM offres 
    WHERE source = 'linkedin'
    ORDER BY collected_at DESC
    LIMIT 5
"""):
    print(f"ID: {row[0]}")
    print(f"Intitule: {repr(row[1])}")
    print(f"Entreprise: {repr(row[2])}")
    print(f"Source: {repr(row[3])}")
    print(f"Score: {row[4]}")
    print(f"Desc length: {row[5]}")
    print("---")

print("\n=== Offres avec description longue mais source non linkedin ===")
for row in conn.execute("""
    SELECT id, intitule, source, LENGTH(description) as desc_len
    FROM offres
    WHERE LENGTH(description) > 200
    AND id LIKE 'linkedin%'
"""):
    print(f"ID: {row[0]} | Source: {repr(row[2])} | Desc: {row[3]} chars")

print("\n=== Stats sources ===")
for row in conn.execute("SELECT source, COUNT(*) FROM offres GROUP BY source"):
    print(f"Source: {repr(row[0])} → {row[1]} offres")
