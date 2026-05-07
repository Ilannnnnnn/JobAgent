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

status, messages = mail.search(None, 'FROM "jobalerts-noreply@linkedin.com"')
ids = messages[0].split()
print(f"Total emails LinkedIn : {len(ids)}")

# Affiche le sujet de chaque email
for email_id in ids[-10:]:  # 10 plus récents
    status, data = mail.fetch(email_id, "(RFC822.HEADER)")
    msg = email.message_from_bytes(data[0][1])
    sujet = msg.get("Subject", "")
    date = msg.get("Date", "")
    print(f"\nID: {email_id.decode()}")
    print(f"Sujet: {sujet}")
    print(f"Date: {date}")

# Cherche spécifiquement les emails avec "alerte" dans le sujet
print("\n\n=== Emails avec 'alerte' dans le sujet ===")
status, messages = mail.search(None, 'FROM "jobalerts-noreply@linkedin.com" SUBJECT "alerte"')
ids_alertes = messages[0].split()
print(f"Trouvés : {len(ids_alertes)}")

for email_id in ids_alertes[-3:]:
    status, data = mail.fetch(email_id, "(RFC822.HEADER)")
    msg = email.message_from_bytes(data[0][1])
    print(f"Sujet: {msg.get('Subject', '')}")
    print(f"Date: {msg.get('Date', '')}")

mail.logout()
