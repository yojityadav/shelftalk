# ShelfTalk backend (FastAPI + SQLite)

One brain, `handle_message(text, source)` in `app/brain.py`, used by the website and Telegram.
Rules and code find the product and read live stock; an LLM (Gemini, then Groq) only rewrites the
reply in a friendlier tone and is optional. Every reply passes a number check, or it becomes a handoff.

## Run
```
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # add keys later; works without them
uvicorn app.main:app --reload                          # http://127.0.0.1:8000/docs
```
Run the tests (no install needed): `python -m unittest discover -s tests -v`

## Try it
```
curl -X POST localhost:8000/api/chat -H "Content-Type: application/json" -d '{"text":"How much are the silver hoops?"}'
curl -X PATCH localhost:8000/api/products/1/stock -H "Content-Type: application/json" -d '{"stock":0}'
curl -X POST localhost:8000/api/chat -H "Content-Type: application/json" -d '{"text":"price of silver hoops"}'   # now: sold out
curl -X POST localhost:8000/api/chat -H "Content-Type: application/json" -d '{"text":"I want a refund"}'
curl localhost:8000/api/queue
curl -X POST localhost:8000/api/queue/1/resolve
```

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | /api/chat | `{text}` -> `{reply, status, reason, note}` |
| GET | /api/products | list with live stock and status |
| PATCH | /api/products/{id}/stock | `{stock}` -> updated product |
| GET | /api/queue | unresolved "Needs owner" items |
| POST | /api/queue/{id}/resolve | mark resolved |
| GET | /api/chats | recent chat log |
| POST | /api/store/connect | `{url}`: Google Sheet or CSV link, or a page with product JSON-LD |
| POST | /api/telegram/webhook | Telegram updates |

## Telegram
1. Put the token from @BotFather in `.env` (`TELEGRAM_BOT_TOKEN`) and set `TELEGRAM_WEBHOOK_SECRET`.
2. Expose the server over HTTPS (deploy it, or use ngrok while testing).
3. Register the webhook once:
   `curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://YOUR-URL/api/telegram/webhook&secret_token=<SECRET>"`

## Data files
- `data/policy.txt`: delivery cities and days, return policy. Edited by hand; read on every message.
- `data/shelftalk.db`: created on first run with 4 sample products.
- Sheet columns for /api/store/connect: `name, price, stock` (optional `aliases, sizes`).

## Behaviour notes
- Stock 0: answers "sold out". Stock 1 or 2: hands off ("the owner will confirm").
- Complaints, refunds, discounts, custom orders, "talk to a human", unknown products, unknown cities: handoff, and a row in the queue.
- Two matching products (e.g. "linen"): asks which one instead of guessing.
- Page imports (JSON-LD) only know in stock vs out of stock, so in-stock items are set to 10; the response says so.
- Set `ALLOWED_ORIGINS` in `.env` to your Netlify URL or the browser will block calls to the API.
