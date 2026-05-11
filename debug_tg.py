import os, sys, json
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()
import httpx

token = os.getenv("TELEGRAM_TOKEN")
r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
updates = r.json().get("result", [])
print(f"Total updates: {len(updates)}")
for u in updates[-10:]:
    msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
    if msg:
        chat = msg.get("chat", {})
        frm  = msg.get("from", {})
        print(f"  chat_id={chat.get('id')}  type={chat.get('type')}  "
              f"user={frm.get('username') or frm.get('first_name')}  "
              f"text={str(msg.get('text',''))[:50]}")
