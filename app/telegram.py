"""Send replies to Telegram. Stdlib only."""
import json, os, urllib.request

def send_message(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json.dumps({"chat_id": chat_id, "text": text}).encode(),
        {"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=8).read()
        return True
    except Exception:
        return False
