"""FastAPI layer. Run: uvicorn app.main:app --reload"""
import os
from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import brain, db, store, telegram


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    yield


app = FastAPI(title="ShelfTalk API", lifespan=lifespan)

origins = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080,https://shelftalk-sandy.vercel.app").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins,
                   allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type"])


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)

class StockIn(BaseModel):
    stock: int = Field(ge=0, le=100000)

class ConnectIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)

class OwnerTelegramRequest(BaseModel):
    owner_username: str

STORE_SETTINGS = {
    "owner_username": "yojityadav"
}

def _view(p):
    s = p["stock"]
    status = "Sold out" if s == 0 else f"Only {s} left" if s <= 2 else "In stock"
    return {"id": p["id"], "name": p["name"], "price": p["price"], "stock": s,
            "sizes": p["sizes"], "status": status, "updated_at": p["updated_at"]}


@app.get("/api/health")
def health():
    return {"ok": True}

@app.post("/api/chat")
def chat(body: ChatIn):
    r = brain.handle_message(body.text, "web")
    return {"reply": r["reply"], "status": r["status"], "reason": r["reason"], "note": r["note"]}

@app.get("/api/products")
def products():
    return [_view(p) for p in db.list_products()]

@app.patch("/api/products/{pid}/stock")
def update_stock(pid: int, body: StockIn):
    if not db.get_product(pid):
        raise HTTPException(404, "Product not found")
    return _view(db.set_stock(pid, body.stock))

@app.get("/api/queue")
def queue():
    return db.list_queue()

@app.post("/api/queue/{qid}/resolve")
def resolve(qid: int):
    if not db.resolve_queue(qid):
        raise HTTPException(404, "Queue item not found")
    return {"ok": True}

@app.get("/api/chats")
def chats():
    return db.list_chats()
@app.post("/api/store/owner-telegram")
def update_owner_telegram(req: OwnerTelegramRequest):
    STORE_SETTINGS["owner_username"] = req.owner_username.replace("@", "")
    return {"ok": True, "owner_username": STORE_SETTINGS["owner_username"]}
@app.post("/api/store/connect")
def connect(body: ConnectIn):
    try:
        return store.import_store(body.url)
    except store.StoreError as e:
        raise HTTPException(422, str(e))

@app.post("/api/telegram/webhook")
async def telegram_webhook(
    update: dict = Body(...),
    x_telegram_bot_api_secret_token: str = Header(default=None),
):
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if secret and x_telegram_bot_api_secret_token != secret:
        raise HTTPException(403, "Bad secret")

    msg = update.get("message") or {}
    text = msg.get("text")
    chat_id = (msg.get("chat") or {}).get("id")

    if text and chat_id:
        r = brain.handle_message(text, "telegram")
        reply_text = r["reply"]

        if r.get("status") == "needs_owner" or "owner" in str(r.get("status", "")).lower():
            owner_handle = STORE_SETTINGS.get("owner_username", "yojityadav")
            payload = {
                "chat_id": chat_id,
                "text": reply_text,
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "💬 Chat with Store Owner", "url": f"https://t.me/{owner_handle}"}]
                    ]
                },
            }
            import httpx
            token = os.getenv("TELEGRAM_BOT_TOKEN")
            async with httpx.AsyncClient() as client:
                await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        else:
            telegram.send_message(chat_id, reply_text)

    return {"ok": True}