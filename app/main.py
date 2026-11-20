"""FastAPI layer for ShelfTalk Live Instagram, Botpress & Store Admin API. Run: uvicorn app.main:app --reload"""
import csv
import datetime
import html as html_lib
import inspect
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
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


def _patch_store_and_db_limits():
    """Removes any hardcoded 4-product slice/limit (e.g. [:4] or LIMIT 4) inside app/store.py and app/db.py."""
    for mod in (store, db):
        try:
            for attr_name, attr_val in list(vars(mod).items()):
                if isinstance(attr_val, int) and attr_val == 4 and any(
                    k in attr_name.lower() for k in ("max", "limit", "count", "cap", "top")
                ):
                    setattr(mod, attr_name, 100)

            src = inspect.getsource(mod)
            patched = src
            # Replace [:4] slices with [:100]
            patched = re.sub(r"\[\s*:\s*4\s*\]", "[:100]", patched)
            # Replace len(...) >= 4 or == 4 loop caps with 100
            patched = re.sub(r"(len\([^)]+\)\s*(?:>=|==|>)\s*)4\b", r"\g<1>100", patched)
            # Replace SQL LIMIT 4 with LIMIT 100
            patched = re.sub(r"\bLIMIT\s+4\b", "LIMIT 100", patched, flags=re.IGNORECASE)
            # Replace range(4) or min(..., 4) product caps
            patched = re.sub(r"\brange\(\s*4\s*\)", "range(100)", patched)

            if patched != src and getattr(mod, "__file__", None):
                code_obj = compile(patched, mod.__file__, "exec")
                exec(code_obj, mod.__dict__)
                print(f"Patched 4-item limit to 100 in {mod.__name__}")
        except Exception as e:
            print(f"Notice while inspecting {getattr(mod, '__name__', 'module')}: {e}")


_patch_store_and_db_limits()

_ORIG_LIST_PRODUCTS = getattr(db, "list_products", None)
_ORIG_GET_PRODUCT = getattr(db, "get_product", None)
_ORIG_SET_STOCK = getattr(db, "set_stock", None)
_CUSTOM_CATALOG: list[dict] = []


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_aliases(name: str) -> str:
    words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", name) if len(w) > 2]
    return ",".join(dict.fromkeys(words))


def _normalize_product_list(items: list[dict]) -> list[dict]:
    normalized = []
    seen = set()
    for idx, item in enumerate(items, start=1):
        name = str(item.get("name") or item.get("title") or "").strip()
        if not name or len(name) < 2:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)

        raw_price = item.get("price", 0)
        if isinstance(raw_price, str):
            m = re.search(r"\d+(?:,\d{3})*(?:\.\d+)?", raw_price)
            raw_price = float(m.group(0).replace(",", "")) if m else 0
        price = int(round(float(raw_price or 0)))

        raw_stock = item.get("stock", 8)
        try:
            stock = max(0, int(float(raw_stock)))
        except Exception:
            stock = 8

        sizes = str(item.get("sizes") or "Standard").strip() or "Standard"
        aliases = str(item.get("aliases") or _make_aliases(name)).strip()
        updated_at = str(item.get("updated_at") or _now_iso())

        normalized.append({
            "id": idx,
            "name": name,
            "price": price,
            "stock": stock,
            "sizes": sizes,
            "aliases": aliases,
            "updated_at": updated_at,
        })
    return normalized


def _persist_products_to_db(items: list[dict]) -> list[dict]:
    """Saves all imported products into db (via db functions, SQLite table, and runtime hooks)."""
    global _CUSTOM_CATALOG
    normalized = _normalize_product_list(items)
    if not normalized:
        return db.list_products()

    # 1. Try any native bulk replace/save function on db
    for fn_name in ("replace_products", "save_products", "set_products", "reset_products", "import_products", "seed_products"):
        fn = getattr(db, fn_name, None)
        if callable(fn):
            try:
                fn(normalized)
            except Exception:
                pass

    # 2. Check if db uses SQLite and write all rows directly so no row is dropped
    try:
        conn = None
        close_after = False
        for conn_fn_name in ("_conn", "conn", "get_conn", "connect", "get_db"):
            conn_fn = getattr(db, conn_fn_name, None)
            if callable(conn_fn):
                res = conn_fn()
                if hasattr(res, "__enter__"):
                    conn = res.__enter__()
                elif isinstance(res, sqlite3.Connection):
                    conn = res
                    close_after = True
                break

        if conn is None:
            for path_attr in ("DB_PATH", "DB_FILE", "PATH", "DB"):
                db_path = getattr(db, path_attr, None)
                if isinstance(db_path, (str, os.PathLike)) and os.path.exists(str(db_path)):
                    conn = sqlite3.connect(str(db_path))
                    close_after = True
                    break

        if conn is not None:
            cur = conn.cursor()
            tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "products" in tables:
                cols_info = cur.execute("PRAGMA table_info(products)").fetchall()
                col_names = [c[1] for c in cols_info]
                cur.execute("DELETE FROM products")
                for p in normalized:
                    row_vals = []
                    for c in col_names:
                        if c in p:
                            row_vals.append(p[c])
                        elif c == "status":
                            s = p["stock"]
                            row_vals.append("Sold out" if s == 0 else f"Only {s} left" if s <= 2 else "In stock")
                        else:
                            row_vals.append("")
                    placeholders = ",".join(["?"] * len(col_names))
                    col_list = ",".join(col_names)
                    cur.execute(f"INSERT INTO products ({col_list}) VALUES ({placeholders})", row_vals)
                conn.commit()
            if close_after:
                conn.close()
    except Exception as e:
        print(f"Direct SQLite sync notice: {e}")

    # 3. Verify db.list_products() returns all imported products; if not, hook db methods
    current = _ORIG_LIST_PRODUCTS() if callable(_ORIG_LIST_PRODUCTS) else []
    if len(current) != len(normalized):
        _CUSTOM_CATALOG = [dict(p) for p in normalized]

        def _custom_list_products():
            return [dict(p) for p in _CUSTOM_CATALOG]

        def _custom_get_product(pid: int):
            for p in _CUSTOM_CATALOG:
                if int(p["id"]) == int(pid):
                    return dict(p)
            return None

        def _custom_set_stock(pid: int, stock: int):
            for p in _CUSTOM_CATALOG:
                if int(p["id"]) == int(pid):
                    p["stock"] = int(stock)
                    p["updated_at"] = _now_iso()
                    return dict(p)
            return None

        db.list_products = _custom_list_products
        db.get_product = _custom_get_product
        db.set_stock = _custom_set_stock
    else:
        _CUSTOM_CATALOG = []
        if callable(_ORIG_LIST_PRODUCTS):
            db.list_products = _ORIG_LIST_PRODUCTS
        if callable(_ORIG_GET_PRODUCT):
            db.get_product = _ORIG_GET_PRODUCT
        if callable(_ORIG_SET_STOCK):
            db.set_stock = _ORIG_SET_STOCK

    return db.list_products()


def _fetch_url_text(url: str, accept: str = "*/*") -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_products_from_json_obj(data) -> list[dict]:
    """Recursively extracts product dicts from Shopify, WooCommerce, Next.js, or custom JSON."""
    results = []

    if isinstance(data, dict):
        # Shopify products.json format
        if isinstance(data.get("products"), list):
            for item in data["products"]:
                if not isinstance(item, dict):
                    continue
                name = item.get("title") or item.get("name")
                if not name:
                    continue
                variants = item.get("variants") if isinstance(item.get("variants"), list) else []
                if variants and isinstance(variants[0], dict):
                    price = variants[0].get("price", item.get("price", 0))
                    sizes = ", ".join(
                        str(v.get("title", "")) for v in variants
                        if isinstance(v, dict) and v.get("title") and v.get("title") != "Default Title"
                    ) or str(item.get("sizes") or "Standard")
                    stock = sum(
                        int(v.get("inventory_quantity", 5) if v.get("inventory_quantity") is not None else (5 if v.get("available", True) else 0))
                        for v in variants if isinstance(v, dict)
                    )
                else:
                    price = item.get("price") or item.get("p") or 0
                    sizes = item.get("sizes") or item.get("size") or "Standard"
                    stock = item.get("stock") if item.get("stock") is not None else (item.get("inventory") or 8)
                results.append({"name": name, "price": price, "stock": stock, "sizes": sizes})
            if results:
                return results

        # Check other common container keys
        for key in ("items", "catalog", "data", "inventory", "results", "edges", "nodes"):
            if key in data and isinstance(data[key], (list, dict)):
                sub = _extract_products_from_json_obj(data[key])
                if len(sub) > len(results):
                    results = sub

        # Single JSON-LD Product or Offer
        if data.get("@type") in ("Product", "IndividualProduct") and (data.get("name") or data.get("title")):
            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price") if isinstance(offers, dict) else data.get("price", 0)
            avail = str(offers.get("availability", "")) if isinstance(offers, dict) else ""
            stock = 0 if "OutOfStock" in avail else 8
            results.append({
                "name": data.get("name") or data.get("title"),
                "price": price or 0,
                "stock": stock,
                "sizes": data.get("size") or "Standard",
            })

        # JSON-LD ItemList
        if data.get("@type") == "ItemList" and isinstance(data.get("itemListElement"), list):
            for el in data["itemListElement"]:
                if isinstance(el, dict):
                    item_node = el.get("item") if isinstance(el.get("item"), dict) else el
                    results.extend(_extract_products_from_json_obj(item_node))

    elif isinstance(data, list):
        candidate_list = []
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title") or item.get("product_name") or item.get("n")
                has_price = any(k in item for k in ("price", "p", "prices", "mrp", "cost", "amount", "variants", "offers"))
                if name and has_price:
                    price = item.get("price") if item.get("price") is not None else item.get("p")
                    if price is None and isinstance(item.get("prices"), dict):
                        raw_p = item["prices"].get("price") or 0
                        minor = int(item["prices"].get("currency_minor_unit", 0))
                        price = float(raw_p) / (10 ** minor) if minor else raw_p
                    if price is None:
                        price = item.get("mrp") or item.get("cost") or item.get("amount") or 0
                    stock = item.get("stock") if item.get("stock") is not None else item.get("s")
                    if stock is None:
                        stock = item.get("inventory") if item.get("inventory") is not None else (0 if item.get("is_in_stock") is False else 8)
                    sizes = item.get("sizes") or item.get("size") or "Standard"
                    if isinstance(sizes, list):
                        sizes = ", ".join(str(x) for x in sizes) or "Standard"
                    candidate_list.append({
                        "name": str(name),
                        "price": price,
                        "stock": stock,
                        "sizes": str(sizes),
                    })
                else:
                    sub = _extract_products_from_json_obj(item)
                    if sub:
                        candidate_list.extend(sub)
        if candidate_list:
            results.extend(candidate_list)

    return _normalize_product_list(results)


def _extract_products_from_html_or_js(raw_html: str, base_url: str) -> list[dict]:
    """Extracts all products from an HTML storefront, JSON-LD, __NEXT_DATA__, inline JS arrays, or product cards."""
    candidates: list[dict] = []

    # 1. JSON-LD blocks & Next.js __NEXT_DATA__
    for script_body in re.findall(r"<script[^>]*>([\s\S]*?)</script>", raw_html, flags=re.IGNORECASE):
        s = script_body.strip()
        if not s:
            continue
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = json.loads(s)
                found = _extract_products_from_json_obj(parsed)
                if len(found) > len(candidates):
                    candidates = found
            except Exception:
                pass

        # 2. Inline JS product array literals: e.g. const products = [ { ... }, ... ]
        for arr_match in re.finditer(r"\[\s*\{[\s\S]{20,25000}?\}\s*\]", s):
            snippet = arr_match.group(0)
            # Extract objects having name/title and price
            obj_items = []
            for obj_m in re.finditer(r"\{([^{}]{10,1500})\}", snippet):
                body = obj_m.group(1)
                name_m = re.search(r"""(?:name|title|product_name|n)\s*:\s*['"`]([^'"`]{2,120})['"`]""", body)
                price_m = re.search(r"""(?:price|p|mrp|cost|amount)\s*:\s*['"`]?₹?\s*(\d+(?:,\d{3})*(?:\.\d+)?)['"`]?""", body)
                if name_m and price_m:
                    stock_m = re.search(r"""(?:stock|s|qty|quantity|inventory)\s*:\s*(\d+)""", body)
                    size_m = re.search(r"""(?:sizes|size)\s*:\s*['"`]([^'"`]+)['"`]""", body)
                    obj_items.append({
                        "name": name_m.group(1).strip(),
                        "price": float(price_m.group(1).replace(",", "")),
                        "stock": int(stock_m.group(1)) if stock_m else 8,
                        "sizes": size_m.group(1).strip() if size_m else "Standard",
                    })
            norm_objs = _normalize_product_list(obj_items)
            if len(norm_objs) > len(candidates):
                candidates = norm_objs

    # 3. Check linked local JS / JSON scripts if we still have <= 4 products
    if len(candidates) <= 4:
        for src_attr in re.findall(r"""<script[^>]+src=['"]([^'"]+)['"]""", raw_html, flags=re.IGNORECASE):
            if any(skip in src_attr for skip in ("googleapis", "gstatic", "analytics", "gtag", "facebook", "cdn")):
                continue
            full_js_url = urllib.parse.urljoin(base_url, src_attr)
            try:
                js_text = _fetch_url_text(full_js_url)
                obj_items = []
                for obj_m in re.finditer(r"\{([^{}]{10,1500})\}", js_text):
                    body = obj_m.group(1)
                    name_m = re.search(r"""(?:name|title|product_name)\s*:\s*['"`]([^'"`]{2,120})['"`]""", body)
                    price_m = re.search(r"""(?:price|mrp|cost|amount)\s*:\s*['"`]?₹?\s*(\d+(?:,\d{3})*(?:\.\d+)?)['"`]?""", body)
                    if name_m and price_m:
                        stock_m = re.search(r"""(?:stock|qty|quantity|inventory)\s*:\s*(\d+)""", body)
                        size_m = re.search(r"""(?:sizes|size)\s*:\s*['"`]([^'"`]+)['"`]""", body)
                        obj_items.append({
                            "name": name_m.group(1).strip(),
                            "price": float(price_m.group(1).replace(",", "")),
                            "stock": int(stock_m.group(1)) if stock_m else 8,
                            "sizes": size_m.group(1).strip() if size_m else "Standard",
                        })
                norm_objs = _normalize_product_list(obj_items)
                if len(norm_objs) > len(candidates):
                    candidates = norm_objs
            except Exception:
                pass

    # 4. HTML DOM Product Cards (headings + currency price like ₹799, Rs. 799, $25, etc.)
    dom_items = []
    # Match blocks around headings <h2>/<h3>/<h4>
    heading_matches = list(re.finditer(r"<(h[234])[^>]*>([\s\S]*?)</\1>", raw_html, flags=re.IGNORECASE))
    for idx, hm in enumerate(heading_matches):
        raw_title = re.sub(r"<[^>]+>", " ", hm.group(2))
        title = html_lib.unescape(re.sub(r"\s+", " ", raw_title)).strip()
        if not title or len(title) < 2 or len(title) > 90:
            continue
        if any(bad in title.lower() for bad in (
            "connect your store", "how it works", "four checks", "where we are headed",
            "try it on your store", "needs owner", "instagram account", "synced products",
            "store instagram handle", "live synced catalog", "pipeline connection",
            "customer", "faq", "about us", "contact", "footer", "newsletter", "cart"
        )):
            continue

        start_pos = hm.end()
        end_pos = heading_matches[idx + 1].start() if idx + 1 < len(heading_matches) else min(len(raw_html), start_pos + 1800)
        window = raw_html[start_pos:min(end_pos, start_pos + 1800)]
        window_text = html_lib.unescape(re.sub(r"<[^>]+>", " ", window))

        price_m = re.search(
            r"(?:₹|Rs\.?|INR|\$|€|£)\s*(\d+(?:,\d{3})*(?:\.\d+)?)|(?:price|mrp)\s*[:=-]?\s*(\d+(?:,\d{3})*(?:\.\d+)?)",
            window_text,
            flags=re.IGNORECASE,
        )
        if not price_m:
            continue
        price_str = price_m.group(1) or price_m.group(2)
        price_val = float(price_str.replace(",", ""))

        # Look for stock number in the card window
        stock_val = 8
        out_m = re.search(r"<output[^>]*>\s*(\d+)\s*</output>", window, flags=re.IGNORECASE)
        if out_m:
            stock_val = int(out_m.group(1))
        elif re.search(r"sold\s*out|out\s*of\s*stock", window_text, flags=re.IGNORECASE):
            stock_val = 0
        else:
            left_m = re.search(r"(?:only\s+|stock\s*[:=-]?\s*)(\d+)\s*(?:left|in\s*stock|available)?", window_text, flags=re.IGNORECASE)
            if left_m:
                stock_val = int(left_m.group(1))

        size_m = re.search(r"sizes?\s*[:=-]\s*([A-Za-z0-9,\s/\-]+)", window_text, flags=re.IGNORECASE)
        sizes_val = size_m.group(1).strip()[:40] if size_m else "Standard"

        dom_items.append({
            "name": title,
            "price": price_val,
            "stock": stock_val,
            "sizes": sizes_val,
        })

    norm_dom = _normalize_product_list(dom_items)
    if len(norm_dom) > len(candidates):
        candidates = norm_dom

    return candidates


def _parse_google_sheet_all_rows(url: str) -> list[dict]:
    """Downloads a public Google Sheet CSV and imports ALL product rows without any 4-row cap."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        return []
    sheet_id = m.group(1)
    gid_m = re.search(r"[#&?]gid=(\d+)", url)
    gid_part = f"&gid={gid_m.group(1)}" if gid_m else ""
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv{gid_part}"

    csv_text = _fetch_url_text(csv_url)
    reader = csv.DictReader(io.StringIO(csv_text))
    items = []
    for row in reader:
        norm_row = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
        name = (
            norm_row.get("name")
            or norm_row.get("product")
            or norm_row.get("product name")
            or norm_row.get("title")
            or norm_row.get("item")
        )
        if not name:
            vals = [v for v in norm_row.values() if v]
            name = vals[0] if vals else ""
        if not name:
            continue
        price = (
            norm_row.get("price")
            or norm_row.get("mrp")
            or norm_row.get("cost")
            or norm_row.get("amount")
            or "0"
        )
        stock = (
            norm_row.get("stock")
            or norm_row.get("qty")
            or norm_row.get("quantity")
            or norm_row.get("inventory")
            or "8"
        )
        sizes = norm_row.get("sizes") or norm_row.get("size") or "Standard"
        aliases = norm_row.get("aliases") or norm_row.get("keywords") or _make_aliases(name)
        items.append({
            "name": name,
            "price": price,
            "stock": stock,
            "sizes": sizes,
            "aliases": aliases,
        })
    return _normalize_product_list(items)


def _scrape_store_products(url: str) -> list[dict]:
    """Scrapes all products from a Store URL (JSON endpoints, Shopify, WooCommerce, HTML DOM, JS bundles)."""
    base = url.rstrip("/")
    parsed_u = urllib.parse.urlparse(base)
    origin = f"{parsed_u.scheme}://{parsed_u.netloc}"
    best: list[dict] = []

    # 1. Try common JSON product endpoints first
    json_endpoints = [
        base if base.endswith(".json") else f"{base}/products.json?limit=250",
        f"{origin}/products.json?limit=250",
        f"{origin}/api/products",
        f"{origin}/data/products.json",
        f"{origin}/wp-json/wc/store/products?per_page=100",
    ]
    seen_ep = set()
    for ep in json_endpoints:
        if ep in seen_ep:
            continue
        seen_ep.add(ep)
        try:
            raw = _fetch_url_text(ep, accept="application/json, text/plain, */*")
            if raw.lstrip().startswith(("{", "[")):
                data = json.loads(raw)
                extracted = _extract_products_from_json_obj(data)
                if len(extracted) > len(best):
                    best = extracted
        except Exception:
            pass

    # 2. Fetch the Store URL itself (could be HTML or JSON)
    try:
        raw_page = _fetch_url_text(url)
        stripped = raw_page.lstrip()
        if stripped.startswith(("{", "[")):
            extracted = _extract_products_from_json_obj(json.loads(stripped))
            if len(extracted) > len(best):
                best = extracted
        else:
            extracted = _extract_products_from_html_or_js(raw_page, url)
            if len(extracted) > len(best):
                best = extracted
    except Exception as e:
        print(f"Store page fetch notice: {e}")

    return best


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    _patch_store_and_db_limits()
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
        "sizes": p.get("sizes", "Standard"),
        "status": status,
        "updated_at": p.get("updated_at", ""),
    }


def _import_store_or_sheet(raw_url: str):
    """Imports ALL products (8, 20, 50+) from either a Store URL or a Google Sheet without any 4-product cap."""
    _patch_store_and_db_limits()
    url = raw_url.strip()
    is_sheet = "docs.google.com/spreadsheets" in url

    store_py_products: list[dict] = []
    store_py_error = None

    # 1. Run store.import_store (now patched with 100-item limit instead of 4)
    try:
        store.import_store(url)
        if callable(_ORIG_LIST_PRODUCTS):
            store_py_products = _ORIG_LIST_PRODUCTS()
        else:
            store_py_products = db.list_products()
    except Exception as e:
        store_py_error = e

    # 2. Also run our unlimited parser so no store or sheet is ever truncated to 4 products
    direct_products: list[dict] = []
    try:
        if is_sheet:
            direct_products = _parse_google_sheet_all_rows(url)
        else:
            direct_products = _scrape_store_products(url)
    except Exception as e:
        print(f"Direct scraper notice: {e}")

    # Pick whichever extracted the larger complete catalog (e.g. all 8 products instead of 4)
    chosen = direct_products if len(direct_products) > len(store_py_products) else store_py_products

    if chosen:
        saved = _persist_products_to_db(chosen)
        STORE_SETTINGS["source_url"] = url
        STORE_SETTINGS["source_type"] = "google_sheet" if is_sheet else "store_url"
        return {
            "ok": True,
            "imported": len(saved),
            "count": len(saved),
            "source_url": url,
            "source_type": STORE_SETTINGS["source_type"],
            "products": [_view(p) for p in saved],
        }

    if store_py_error:
        raise HTTPException(status_code=422, detail=f"Could not sync products from link: {store_py_error}")
    raise HTTPException(status_code=422, detail="No products found at the provided Store URL or Google Sheet.")


def _query_botpress_or_brain(user_id: str, text: str) -> dict:
    """Forwards incoming message + full synced catalog to Botpress if configured, with local catalog brain fallback."""
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
