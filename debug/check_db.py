import sqlite3

conn = sqlite3.connect("data/offers.db")

print("=== 5 offres LinkedIn en DB ===")
for row in conn.execute("""
    SELECT id, intitule, entreprise_nom, lieu_travail, url, score 
    FROM offres 
    WHERE source = 'linkedin' 
    LIMIT 5
"""):
    print("ID:", row[0])
    print("Intitule:", repr(row[1]))
    print("Entreprise:", repr(row[2]))
    print("Lieu:", repr(row[3]))
    print("URL:", repr(row[4]))
    print("Score:", row[5])
    print("---")

print("\n=== Stats intitulés LinkedIn ===")
for row in conn.execute("""
    SELECT 
        COUNT(*) as total,
        SUM(CASE WHEN intitule = '' OR intitule IS NULL THEN 1 ELSE 0 END) as vides,
        SUM(CASE WHEN intitule LIKE 'http%' THEN 1 ELSE 0 END) as urls,
        SUM(CASE WHEN intitule != '' AND intitule NOT LIKE 'http%' THEN 1 ELSE 0 END) as valides
    FROM offres WHERE source = 'linkedin'
"""):
    print(f"Total: {row[0]}, Vides: {row[1]}, URLs: {row[2]}, Valides: {row[3]}")