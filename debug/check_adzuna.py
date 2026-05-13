import sqlite3, os
from dotenv import load_dotenv
load_dotenv(override=True)

conn = sqlite3.connect(os.getenv("DB_PATH", "data/offers.db"))

print("=== 5 dernières offres Adzuna ===")
for r in conn.execute("""
    SELECT id, intitule, collected_at 
    FROM offres WHERE source='adzuna' 
    ORDER BY collected_at DESC LIMIT 5
"""):
    print(r[0], "|", r[1][:40], "|", r[2])

print("\n=== Total Adzuna en DB ===")
total = conn.execute("SELECT COUNT(*) FROM offres WHERE source='adzuna'").fetchone()[0]
print(f"Total : {total}")

print("\n=== Adzuna des 7 derniers jours ===")
recent = conn.execute("""
    SELECT COUNT(*) FROM offres 
    WHERE source='adzuna' 
    AND collected_at >= date('now', '-7 days')
""").fetchone()[0]
print(f"Récentes (7j) : {recent}")
