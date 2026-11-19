"""FastAPI layer for ShelfTalk Instagram DM Simulator. Run: uvicorn app.main:app --reload"""
import os
from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import brain, db, store


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    yield


app = FastAPI(title="ShelfTalk Instagram DM Simulator API", lifespan=lifespan)

origins = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080,https://shelftalk-sandy.vercel.app").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class StockIn(BaseModel):
    stock: int = Field(ge=0, le=100000)


class ConnectIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)


class OwnerHandleRequest(BaseModel):
    owner_username: str


STORE_SETTINGS = {
    "owner_username": "yojityadav"
}


def _view(p):
    s = p["stock"]
    status = "Sold out" if s == 0 else f"Only {s} left" if s <= 2 else "In stock"
    return {
        "id": p["id"],
        "name": p["name"],
        "price": p["price"],
        "stock": s,
        "sizes": p["sizes"],
        "status": status,
        "updated_at": p["updated_at"],
    }


@app.get("/api/health")
def health():
    return {"ok": True, "mode": "instagram_simulator", "owner_username": STORE_SETTINGS["owner_username"]}


@app.post("/api/chat")
def chat(body: ChatIn):
    r = brain.handle_message(body.text, "instagram")
    owner = STORE_SETTINGS.get("owner_username", "yojityadav")
    reply_text = r["reply"]
    if r.get("status") == "needs_owner":
        reply_text = f"{reply_text} (Bot paused · Handed off to @{owner})"
    return {
        "reply": reply_text,
        "status": r["status"],
        "reason": r["reason"],
        "note": r["note"],
        "owner_username": owner,
    }


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
@app.post("/api/store/owner-instagram")
def update_owner_handle(req: OwnerHandleRequest):
    clean_name = req.owner_username.replace("@", "").strip()
    STORE_SETTINGS["owner_username"] = clean_name
    return {"ok": True, "owner_username": clean_name}


@app.post("/api/store/connect")
def connect(body: ConnectIn):
    try:
        return store.import_store(body.url)
    except store.StoreError as e:
        raise HTTPException(422, str(e))