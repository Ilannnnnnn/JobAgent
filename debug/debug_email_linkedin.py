import imaplib
import email
import os
from dotenv import load_dotenv

load_dotenv()

gmail_address = os.getenv("GMAIL_ADDRESS")
app_password = os.getenv("GMAIL_APP_PASSWORD")

mail = imaplib.IMAP4_SSL("imap.gmail.com")
mail.login(gmail_address, app_password)
mail.select("INBOX")

# Cherche tous les emails LinkedIn (lus et non lus)
status, messages = mail.search(None, 'FROM "jobalerts-noreply@linkedin.com"')
ids = messages[0].split()

if not ids:
    print("Aucun email LinkedIn trouvé")
else:
    # Prend le plus récent
    email_id = ids[-1]
    status, data = mail.fetch(email_id, "(RFC822)")
    msg = email.message_from_bytes(data[0][1])

    corps_html = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                corps_html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                break
    else:
        corps_html = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

    # Sauvegarde le HTML pour inspection
    with open("email_linkedin_debug.html", "w", encoding="utf-8") as f:
        f.write(corps_html)
    print(f"HTML sauvegardé dans email_linkedin_debug.html ({len(corps_html)} chars)")

    # Affiche un extrait du HTML autour des liens d'offres
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(corps_html, "html.parser")

    print("\n=== Liens d'offres trouvés ===")
    count = 0
    for a in soup.find_all("a", href=True):
        if "jobs/view" in a["href"]:
            print(f"\nURL: {a['href'][:80]}")
            print(f"Texte du lien: {repr(a.get_text(strip=True)[:100])}")
            parent = a.find_parent()
            if parent:
                print(f"Tag parent: {parent.name}")
                print(f"Texte parent (200 chars): {repr(parent.get_text(separator='|', strip=True)[:200])}")
            count += 1
            if count >= 3:
                break

mail.logout()
