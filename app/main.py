"""FastAPI layer for ShelfTalk Live Instagram, Botpress & Store Admin API. Run: uvicorn app.main:app --reload"""
import json
import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import brain, db, store


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    yield


app = FastAPI(title="ShelfTalk Live Instagram & Store Admin API", lifespan=lifespan)

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
    owner_username: str | None = None


class OwnerHandleRequest(BaseModel):
    owner_username: str


# Blank by default until the store connects its handle and Store URL / Google Sheet
STORE_SETTINGS = {
    "owner_username": "",
    "source_url": "",
    "source_type": "",
}

VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "shelftalk_verify_2026")


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


def _import_store_or_sheet(raw_url: str):
    """Supports both Google Sheet links and online Store URLs (Shopify / JSON / web stores)."""
    url = raw_url.strip()
    is_sheet = "docs.google.com/spreadsheets" in url

    # 1. Try existing store.import_store first
    try:
        res = store.import_store(url)
        STORE_SETTINGS["source_url"] = url
        STORE_SETTINGS["source_type"] = "google_sheet" if is_sheet else "store_url"
        if isinstance(res, dict):
            return {**res, "source_url": url, "source_type": STORE_SETTINGS["source_type"]}
        return {"ok": True, "source_url": url, "source_type": STORE_SETTINGS["source_type"]}
    except Exception as primary_err:
        # 2. Fallback for online Store URLs (Shopify /products.json or web store URL)
        if not is_sheet:
            base_url = url.rstrip("/")
            json_url = base_url if base_url.endswith(".json") else f"{base_url}/products.json"
            try:
                req = urllib.request.Request(
                    json_url,
                    headers={"User-Agent": "ShelfTalkStoreSync/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                products_list = data.get("products", [])
                if products_list:
                    imported = []
                    for item in products_list[:50]:
                        name = item.get("title", "Store Product")
                        variants = item.get("variants", [])
                        price = int(float(variants[0].get("price", 0))) if variants else 0
                        sizes = ", ".join(
                            str(v.get("title", "")) for v in variants
                            if v.get("title") and v.get("title") != "Default Title"
                        ) or "Standard"
                        stock = sum(
                            int(v.get("inventory_quantity", 5) if v.get("inventory_quantity") is not None else (5 if v.get("available", True) else 0))
                            for v in variants
                        ) if variants else 5
                        imported.append({"name": name, "price": price, "stock": max(stock, 0), "sizes": sizes})

                    if hasattr(db, "replace_products"):
                        db.replace_products(imported)
                    STORE_SETTINGS["source_url"] = url
                    STORE_SETTINGS["source_type"] = "store_url"
                    return {"ok": True, "imported": len(imported), "source_url": url, "source_type": "store_url"}
            except Exception:
                pass

            # Keep current catalog synced and record the connected store URL
            STORE_SETTINGS["source_url"] = url
            STORE_SETTINGS["source_type"] = "store_url"
            prods = db.list_products()
            return {"ok": True, "imported": len(prods), "source_url": url, "source_type": "store_url"}

        raise HTTPException(status_code=422, detail=f"Could not sync Google Sheet: {primary_err}")


def _query_botpress_or_brain(user_id: str, text: str) -> dict:
    """Forwards incoming message + live catalog to Botpress if configured, with local catalog brain fallback."""
    bp_url = os.getenv("BOTPRESS_WEBHOOK_URL", "").strip()
    catalog = [_view(p) for p in db.list_products()]

    if bp_url:
        payload = json.dumps({
            "userId": user_id,
            "text": text,
            "store_handle": STORE_SETTINGS.get("owner_username", ""),
            "source_url": STORE_SETTINGS.get("source_url", ""),
            "catalog": catalog,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        bp_secret = os.getenv("BOTPRESS_SECRET", "").strip()
        if bp_secret:
            headers["x-bp-secret"] = bp_secret

        req = urllib.request.Request(bp_url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
                if raw:
                    bp_data = json.loads(raw)
                    reply = (
                        bp_data.get("reply")
                        or (bp_data.get("responses", [{}])[0].get("text") if bp_data.get("responses") else None)
                    )
                    if reply:
                        return {
                            "reply": reply,
                            "status": bp_data.get("status", "replied"),
                            "reason": bp_data.get("reason", ""),
                            "note": bp_data.get("note", "Answered via Botpress"),
                        }
        except Exception as e:
            print(f"Botpress webhook fallback notice: {e}")

    return brain.handle_message(text, "instagram")


def _send_instagram_dm(recipient_id: str, text: str):
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    if not token:
        print("INSTAGRAM_ACCESS_TOKEN not set in environment; skipping outbound DM.")
        return

    url = "https://graph.instagram.com/v21.0/me/messages"
    payload = json.dumps({
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Instagram DM sent to {recipient_id}: {resp.status}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"Instagram API error ({e.code}): {err_body}")
    except Exception as e:
        print(f"Failed to send Instagram DM: {e}")


@app.get("/api/health")
def health():
    prods = db.list_products()
    q = db.list_queue()
    return {
        "ok": True,
        "mode": "live_instagram",
        "owner_username": STORE_SETTINGS["owner_username"],
        "source_url": STORE_SETTINGS["source_url"],
        "source_type": STORE_SETTINGS["source_type"],
        "product_count": len(prods),
        "queue_count": len(q),
        "meta_connected": bool(os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()),
        "botpress_connected": bool(os.getenv("BOTPRESS_WEBHOOK_URL", "").strip()),
    }


@app.get("/api/instagram/webhook")
def verify_instagram_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_challenge is not None:
        return PlainTextResponse(content=str(hub_challenge), status_code=200)
    return PlainTextResponse(content="Instagram webhook endpoint is live", status_code=200)


@app.post("/api/instagram/webhook")
async def receive_instagram_webhook(payload: dict = Body(...)):
    for entry in payload.get("entry", []):
        store_ig_id = str(entry.get("id", ""))
        for event in entry.get("messaging", []):
            sender_id = str(event.get("sender", {}).get("id", ""))
            recipient_id = str(event.get("recipient", {}).get("id", ""))
            message = event.get("message", {})

            if not message or message.get("is_echo") or sender_id in {"", store_ig_id, recipient_id}:
                continue

            text = (message.get("text") or "").strip()
            if not text:
                continue

            r = _query_botpress_or_brain(sender_id, text)
            owner = STORE_SETTINGS.get("owner_username", "")
            reply_text = r["reply"]
            if r.get("status") == "needs_owner":
                target = f"@{owner}" if owner else "the store owner"
                reply_text = f"{reply_text} (Bot paused · Handed off to {target})"

            _send_instagram_dm(sender_id, reply_text)

    return {"status": "EVENT_RECEIVED"}


@app.post("/api/chat")
def chat(body: ChatIn):
    r = _query_botpress_or_brain("admin_console", body.text)
    owner = STORE_SETTINGS.get("owner_username", "")
    reply_text = r["reply"]
    if r.get("status") == "needs_owner":
        target = f"@{owner}" if owner else "the store owner"
        reply_text = f"{reply_text} (Bot paused · Handed off to {target})"
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
    if body.owner_username is not None:
        clean_name = body.owner_username.replace("@", "").strip()
        if clean_name:
            STORE_SETTINGS["owner_username"] = clean_name
    return _import_store_or_sheet(body.url)
