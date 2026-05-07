import os, sqlite3, sys
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, "src")
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

db_path = os.getenv("DB_PATH", "data/offers.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

offre = conn.execute(
    "SELECT * FROM offres WHERE id = 'linkedin_87bb2f0dbfea'"
).fetchone()

print(f"Offre : {offre['intitule']}")
print(f"Description ({len(offre['description'])} chars) : {offre['description'][:200]}")

prompt = f"""Score cette offre pour ce candidat. Retourne UNIQUEMENT ce JSON :
{{"score": 75, "explication": "test", "points_forts": ["a"], "points_faibles": ["b"]}}

Offre : {offre['intitule']} chez {offre['entreprise_nom']}
Description : {offre['description'][:1000]}
"""

print(f"\nPrompt length : {len(prompt)} chars")
print("Appel Haiku...")

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=1024,
    messages=[{"role": "user", "content": prompt}]
)

print(f"Stop reason : {response.stop_reason}")
print(f"Content blocks : {len(response.content)}")
if response.content:
    print(f"Reponse brute : {repr(response.content[0].text[:500])}")
else:
    print("REPONSE VIDE !")
