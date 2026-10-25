"""Run locally once: python gen_session.py  -> prints TG_SESSION_STRING for your .env / Render."""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API ID: "))
api_hash = input("API HASH: ")
with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\nTG_SESSION_STRING=" + c.session.save())
