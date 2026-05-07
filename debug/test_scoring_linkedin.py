import os, sqlite3, sys, time
from dotenv import load_dotenv
load_dotenv(override=True)

db_path = os.getenv("DB_PATH", "data/offers.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

sys.path.insert(0, "src")
from scorer import scorer_offre, mettre_a_jour_score, formater_profil
import yaml

with open("config/profile.yaml", encoding="utf-8") as f:
    profil = yaml.safe_load(f)
profil_texte = formater_profil(profil)

# Toutes les offres buguées : score -1, NULL, ou 0 avec un titre valide
rows = conn.execute("""
    SELECT * FROM offres
    WHERE (score = -1 OR score IS NULL OR score = 0)
    AND intitule != ''
    AND intitule IS NOT NULL
    AND intitule NOT LIKE 'http%'
""").fetchall()

print(f"{len(rows)} offres buguees a rescorer")
print()

ok = 0
erreurs = 0

for i, offre_row in enumerate(rows):
    d = dict(offre_row)
    print(f"[{i+1}/{len(rows)}] {d['intitule']} ({d.get('source','?')})")
    try:
        score, explication, points_forts, points_faibles = scorer_offre(offre_row, profil_texte)
        mettre_a_jour_score(d["id"], score, explication, points_forts, points_faibles, db_path)
        print(f"  -> Score : {score}/100")
        ok += 1
    except Exception as e:
        import traceback
        print(f"  -> ERREUR : {e}")
        traceback.print_exc()
        erreurs += 1
    time.sleep(1)

print()
print(f"Rescoring termine : {ok} OK, {erreurs} erreurs")