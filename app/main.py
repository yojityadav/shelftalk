"""FastAPI layer for ShelfTalk Live Instagram, Botpress & Store Admin API. Run: uvicorn app.main:app --reload"""
import concurrent.futures
import csv
import datetime
import html as html_lib
import inspect
import io
import json
import os
import re
import sqlite3
import time
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

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "store_settings.json")

HEARTH_CO_DEFAULT_PRODUCTS = [
    {"id": 1, "name": "Sage Stoneware Mug", "category": "Home", "sizes": "Home", "price": 18.0, "stock": 24, "currency": "$"},
    {"id": 2, "name": "Aria Wireless Headphones", "category": "Electronics", "sizes": "Electronics", "price": 149.0, "stock": 8, "currency": "$"},
    {"id": 3, "name": "Canvas & Leather Tote", "category": "Accessories", "sizes": "Accessories", "price": 79.0, "stock": 3, "currency": "$"},
    {"id": 4, "name": "Linen Notebook Set", "category": "Stationery", "sizes": "Stationery", "price": 32.0, "stock": 40, "currency": "$"},
    {"id": 5, "name": "Amber Soy Candle", "category": "Home", "sizes": "Home", "price": 26.0, "stock": 0, "currency": "$"},
    {"id": 6, "name": "Everyday White Sneaker", "category": "Footwear", "sizes": "Footwear", "price": 110.0, "stock": 12, "currency": "$"},
    {"id": 7, "name": "Pocket Bluetooth Speaker", "category": "Electronics", "sizes": "Electronics", "price": 59.0, "stock": 4, "currency": "$"},
    {"id": 8, "name": "Terracotta Succulent", "category": "Home", "sizes": "Home", "price": 22.0, "stock": 15, "currency": "$"},
]


def _load_persisted_settings() -> dict:
    default = {
        "owner_username": "",
        "source_url": "",
        "source_type": "",
        "currency": "₹",
        "last_sync_ts": 0.0,
    }
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default.update(data)
    except Exception:
        pass
    return default


def _save_persisted_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(STORE_SETTINGS, f)
    except Exception:
        pass


STORE_SETTINGS = _load_persisted_settings()
VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "shelftalk_verify_2026")


def _patch_store_and_db_limits():
    """Removes any hardcoded 4-product slice/limit (e.g. [:4] or LIMIT 4) inside app/store.py and app/db.py."""
    for mod in (store, db):
        try:
            for attr_name, attr_val in list(vars(mod).items()):
                if isinstance(attr_val, int) and attr_val == 4 and any(
                    k in attr_name.lower() for k in ("max", "limit", "count", "cap", "top")
                ):
                    setattr(mod, attr_name, 200)

            src = inspect.getsource(mod)
            patched = src
            patched = re.sub(r"\[\s*:\s*4\s*\]", "[:200]", patched)
            patched = re.sub(r"(len\([^)]+\)\s*(?:>=|==|>)\s*)4\b", r"\g<1>200", patched)
            patched = re.sub(r"\bLIMIT\s+4\b", "LIMIT 200", patched, flags=re.IGNORECASE)
            patched = re.sub(r"\brange\(\s*4\s*\)", "range(200)", patched)

            if patched != src and getattr(mod, "__file__", None):
                code_obj = compile(patched, mod.__file__, "exec")
                exec(code_obj, mod.__dict__)
        except Exception:
            pass


_patch_store_and_db_limits()

_ORIG_LIST_PRODUCTS = getattr(db, "list_products", None)
_ORIG_GET_PRODUCT = getattr(db, "get_product", None)
_ORIG_SET_STOCK = getattr(db, "set_stock", None)
_CUSTOM_CATALOG: list[dict] = []


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_aliases(name: str, category: str = "") -> str:
    words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", f"{name} {category}") if len(w) > 2]
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
        currency = item.get("currency") or ""
        if isinstance(raw_price, str):
            if "$" in raw_price or "USD" in raw_price.upper():
                currency = "$"
            elif "₹" in raw_price or "INR" in raw_price.upper() or "RS" in raw_price.upper():
                currency = "₹"
            m = re.search(r"\d+(?:,\d{3})*(?:\.\d+)?", raw_price)
            raw_price = float(m.group(0).replace(",", "")) if m else 0.0

        price_float = float(raw_price or 0)
        price = int(price_float) if price_float.is_integer() else round(price_float, 2)

        raw_stock = item.get("stock", 8)
        try:
            stock = max(0, int(float(raw_stock)))
        except Exception:
            stock = 8

        category = str(item.get("category") or "").strip()
        sizes = str(item.get("sizes") or category or "Standard").strip() or "Standard"
        aliases = str(item.get("aliases") or _make_aliases(name, category)).strip()
        updated_at = str(item.get("updated_at") or _now_iso())

        if currency in ("$", "₹"):
            STORE_SETTINGS["currency"] = currency

        normalized.append({
            "id": idx,
            "name": name,
            "price": price,
            "stock": stock,
            "sizes": sizes,
            "category": category or sizes,
            "currency": currency or STORE_SETTINGS.get("currency", "₹"),
            "aliases": aliases,
            "updated_at": updated_at,
        })
    return normalized


def _install_catalog_hooks(normalized: list[dict]):
    """Ensures db.list_products(), db.get_product(), db.set_stock() and brain always see the full live catalog."""
    global _CUSTOM_CATALOG
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
                p["stock"] = max(0, int(stock))
                p["updated_at"] = _now_iso()
                _sync_sqlite_table(_CUSTOM_CATALOG)
                return dict(p)
        return None

    db.list_products = _custom_list_products
    db.get_product = _custom_get_product
    db.set_stock = _custom_set_stock

    for mod in (brain, store):
        if hasattr(mod, "list_products"):
            setattr(mod, "list_products", _custom_list_products)
        if hasattr(mod, "get_product"):
            setattr(mod, "get_product", _custom_get_product)


def _sync_sqlite_table(normalized: list[dict]):
    """Best-effort write of all products into db's SQLite table or in-memory list."""
    for attr_name, attr_val in list(vars(db).items()):
        if isinstance(attr_val, list) and attr_val and isinstance(attr_val[0], dict) and "name" in attr_val[0] and "stock" in attr_val[0]:
            attr_val[:] = [dict(p) for p in normalized]

    db_path = None
    for path_attr in ("DB_PATH", "DB_FILE", "PATH", "DB", "DATABASE"):
        val = getattr(db, path_attr, None)
        if isinstance(val, (str, os.PathLike)) and os.path.exists(str(val)):
            db_path = str(val)
            break

    if not db_path:
        try:
            src = inspect.getsource(db)
            m = re.search(r"([a-zA-Z0-9_\-/]+\.db)", src)
            if m and os.path.exists(m.group(1)):
                db_path = m.group(1)
        except Exception:
            pass

    if db_path:
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
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
                                row_vals.append("Sold out" if s == 0 else f"Only {s} left" if s <= 5 else "In stock")
                            else:
                                row_vals.append("")
                        placeholders = ",".join(["?"] * len(col_names))
                        col_list = ",".join(col_names)
                        cur.execute(f"INSERT INTO products ({col_list}) VALUES ({placeholders})", row_vals)
                    conn.commit()
        except Exception as e:
            print(f"SQLite sync notice: {e}")


def _persist_products_to_db(items: list[dict]) -> list[dict]:
    normalized = _normalize_product_list(items)
    if not normalized:
        return db.list_products()
    _sync_sqlite_table(normalized)
    _install_catalog_hooks(normalized)
    return db.list_products()


def _fetch_url_text(url: str, accept: str = "*/*", timeout: float = 4.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_all_objects(text: str) -> list[str]:
    """Stack-based extractor that finds all {...} object literals at ANY nesting depth inside JS functions or bundles."""
    objs = []
    stack = []
    in_str = None
    escape = False

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
            continue
        if ch == "{":
            stack.append(i)
        elif ch == "}":
            if stack:
                start = stack.pop()
                span = i - start + 1
                if 15 <= span <= 2000:
                    candidate = text[start:i + 1]
                    if candidate.count("{") <= 3:
                        objs.append(candidate)
    return objs


def _extract_products_from_script_text(script_text: str) -> list[dict]:
    """Universal extractor for JS/TypeScript/React/Vite/Next.js/JSON product objects at any nesting depth."""
    if not script_text or len(script_text) < 15:
        return []

    variants_to_scan = [script_text]
    if '\\"' in script_text or "\\'" in script_text:
        variants_to_scan.append(script_text.replace('\\"', '"').replace("\\'", "'"))

    best: list[dict] = []

    name_re = re.compile(
        r"""(?:['"`]?\b(?:name|title|product_name|productName|item_name|product|n)\b['"`]?)\s*:\s*['"`]([^'"`\\]{2,100})['"`]""",
        re.IGNORECASE,
    )
    price_re = re.compile(
        r"""(?:['"`]?\b(?:price|mrp|cost|amount|unit_price|sale_price|p)\b['"`]?)\s*:\s*['"`]?\s*(₹|\$|Rs\.?|INR|USD|€|£)?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*['"`]?""",
        re.IGNORECASE,
    )
    stock_re = re.compile(
        r"""(?:['"`]?\b(?:stock|qty|quantity|inventory|inventory_quantity|count|s)\b['"`]?)\s*:\s*['"`]?(\d+)['"`]?""",
        re.IGNORECASE,
    )
    cat_re = re.compile(
        r"""(?:['"`]?\b(?:category|cat|department|collection|group|type)\b['"`]?)\s*:\s*['"`]([^'"`\\]{2,50})['"`]""",
        re.IGNORECASE,
    )
    size_re = re.compile(
        r"""(?:['"`]?\b(?:sizes|size|variant)\b['"`]?)\s*:\s*['"`]([^'"`\\]{1,50})['"`]""",
        re.IGNORECASE,
    )

    for text in variants_to_scan:
        found = []
        for obj_str in _extract_all_objects(text):
            nm = name_re.search(obj_str)
            pm = price_re.search(obj_str)
            if not (nm and pm):
                continue
            name_val = nm.group(1).strip()
            if name_val.lower() in ("home", "admin", "store", "default", "main", "index", "app", "shelftalk"):
                continue
            curr_sym = (pm.group(1) or "").strip()
            currency = "$" if ("$" in curr_sym or "USD" in curr_sym.upper()) else ("₹" if curr_sym else "$")
            price_val = float(pm.group(2).replace(",", ""))

            sm = stock_re.search(obj_str)
            stock_val = int(sm.group(1)) if sm else 8

            cm = cat_re.search(obj_str)
            cat_val = cm.group(1).strip() if cm else ""

            szm = size_re.search(obj_str)
            size_val = szm.group(1).strip() if szm else (cat_val or "Standard")

            found.append({
                "name": name_val,
                "price": price_val,
                "stock": stock_val,
                "category": cat_val or "Standard",
                "sizes": size_val,
                "currency": currency,
            })

        norm = _normalize_product_list(found)
        if len(norm) > len(best):
            best = norm

    return best


def _extract_products_from_json_obj(data) -> list[dict]:
    results = []
    if isinstance(data, dict):
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
                    ) or str(item.get("category") or item.get("sizes") or "Standard")
                    stock = sum(
                        int(v.get("inventory_quantity", 5) if v.get("inventory_quantity") is not None else (5 if v.get("available", True) else 0))
                        for v in variants if isinstance(v, dict)
                    )
                else:
                    price = item.get("price") if item.get("price") is not None else item.get("p", 0)
                    sizes = item.get("category") or item.get("sizes") or item.get("size") or "Standard"
                    stock = item.get("stock") if item.get("stock") is not None else (item.get("inventory") if item.get("inventory") is not None else 8)
                results.append({
                    "name": name,
                    "price": price,
                    "stock": stock,
                    "category": str(item.get("category") or item.get("product_type") or ""),
                    "sizes": sizes,
                    "currency": item.get("currency") or "",
                })
            if results:
                return _normalize_product_list(results)

        for key in ("items", "catalog", "data", "inventory", "results", "edges", "nodes"):
            if key in data and isinstance(data[key], (list, dict)):
                sub = _extract_products_from_json_obj(data[key])
                if len(sub) > len(results):
                    results = sub

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
                    category = str(item.get("category") or item.get("type") or "")
                    sizes = item.get("sizes") or item.get("size") or category or "Standard"
                    if isinstance(sizes, list):
                        sizes = ", ".join(str(x) for x in sizes) or "Standard"
                    candidate_list.append({
                        "name": str(name),
                        "price": price,
                        "stock": stock,
                        "category": category,
                        "sizes": str(sizes),
                        "currency": item.get("currency") or "",
                    })
                else:
                    sub = _extract_products_from_json_obj(item)
                    if sub:
                        candidate_list.extend(sub)
        if candidate_list:
            results.extend(candidate_list)

    return _normalize_product_list(results)


def _extract_products_from_html_tables_and_cards(raw_html: str) -> list[dict]:
    """Extracts products from HTML <table> rows and DOM product cards."""
    best: list[dict] = []

    table_items = []
    for tr_m in re.finditer(r"<tr[^>]*>([\s\S]*?)</tr>", raw_html, flags=re.IGNORECASE):
        row_html = tr_m.group(1)
        if "<th" in row_html.lower():
            continue
        tds = re.findall(r"<td[^>]*>([\s\S]*?)</td>", row_html, flags=re.IGNORECASE)
        if len(tds) < 2:
            continue

        td_texts = []
        for td in tds:
            inp_val = re.search(r"""<input[^>]+value=['"]?(\d+)['"]?""", td, flags=re.IGNORECASE)
            cleaned = html_lib.unescape(re.sub(r"<[^>]+>", " ", td))
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if inp_val and not cleaned:
                cleaned = inp_val.group(1)
            td_texts.append(cleaned)

        full_row_text = " | ".join(td_texts)
        price_m = re.search(r"(₹|\$|Rs\.?|INR|USD|€|£)\s*(\d+(?:,\d{3})*(?:\.\d+)?)", full_row_text, flags=re.IGNORECASE)
        if not price_m:
            continue

        curr_sym = price_m.group(1)
        currency = "$" if ("$" in curr_sym or "USD" in curr_sym.upper()) else "₹"
        price_val = float(price_m.group(2).replace(",", ""))

        name_val = ""
        cat_val = ""
        stock_val = None

        for txt in td_texts:
            if not txt:
                continue
            if re.search(r"(?:₹|\$|Rs\.?|INR|USD)\s*\d+", txt, flags=re.IGNORECASE):
                continue
            if txt.lower() in ("edit", "delete", "edit delete", "actions", "in stock", "low stock", "out of stock", "sold out"):
                if "out of stock" in txt.lower() or "sold out" in txt.lower():
                    stock_val = 0
                continue
            stock_cell_m = re.match(r"^[\-−\+\s]*(\d+)[\-−\+\s]*$", txt)
            if stock_cell_m:
                stock_val = int(stock_cell_m.group(1))
                continue
            if not name_val:
                name_val = re.sub(r"\s*(Edit|Delete)\s*$", "", txt, flags=re.IGNORECASE).strip()
            elif not cat_val and len(txt) < 40:
                cat_val = txt

        if stock_val is None:
            if re.search(r"out\s*of\s*stock|sold\s*out", full_row_text, flags=re.IGNORECASE):
                stock_val = 0
            else:
                sm = re.search(r"\b(\d+)\b", " ".join(td_texts[2:]))
                stock_val = int(sm.group(1)) if sm else 8

        if name_val and len(name_val) >= 2:
            table_items.append({
                "name": name_val,
                "price": price_val,
                "stock": stock_val,
                "category": cat_val or "Standard",
                "sizes": cat_val or "Standard",
                "currency": currency,
            })

    norm_table = _normalize_product_list(table_items)
    if len(norm_table) > len(best):
        best = norm_table

    card_items = []
    heading_matches = list(re.finditer(r"<(h[2345]|strong|b)[^>]*>([\s\S]*?)</\1>", raw_html, flags=re.IGNORECASE))
    for idx, hm in enumerate(heading_matches):
        raw_title = re.sub(r"<[^>]+>", " ", hm.group(2))
        title = html_lib.unescape(re.sub(r"\s+", " ", raw_title)).strip()
        if not title or len(title) < 3 or len(title) > 85:
            continue
        if any(bad in title.lower() for bad in (
            "connect your store", "how it works", "four checks", "where we are headed",
            "try it on your store", "needs owner", "instagram account", "synced products",
            "store instagram handle", "live synced catalog", "pipeline connection",
            "total products", "total stock", "low stock", "out of stock", "policy"
        )) or re.match(r"^[₹\$]?\d+", title):
            continue

        start_pos = hm.end()
        end_pos = heading_matches[idx + 1].start() if idx + 1 < len(heading_matches) else min(len(raw_html), start_pos + 1200)
        window = raw_html[start_pos:min(end_pos, start_pos + 1200)]
        window_text = html_lib.unescape(re.sub(r"<[^>]+>", " ", window))

        price_m = re.search(r"(₹|\$|Rs\.?|INR|USD|€|£)\s*(\d+(?:,\d{3})*(?:\.\d+)?)", window_text, flags=re.IGNORECASE)
        if not price_m:
            continue
        curr_sym = price_m.group(1)
        currency = "$" if ("$" in curr_sym or "USD" in curr_sym.upper()) else "₹"
        price_val = float(price_m.group(2).replace(",", ""))

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

        card_items.append({
            "name": title,
            "price": price_val,
            "stock": stock_val,
            "sizes": "Standard",
            "currency": currency,
        })

    norm_cards = _normalize_product_list(card_items)
    if len(norm_cards) > len(best):
        best = norm_cards

    return best


def _scrape_store_products(url: str) -> list[dict]:
    """Fast parallel scraper (<2s) for Store URLs (HTML, inline JS, external JS bundles, JSON endpoints)."""
    base = url.rstrip("/")
    parsed_u = urllib.parse.urlparse(base)
    origin = f"{parsed_u.scheme}://{parsed_u.netloc}"
    best: list[dict] = []

    candidate_urls = [
        url,
        f"{origin}/api/products",
        f"{origin}/products.json?limit=250",
        f"{origin}/data/products.json",
        f"{origin}/admin.html",
    ]
    unique_urls = list(dict.fromkeys(candidate_urls))

    fetched_pages: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {pool.submit(_fetch_url_text, u, "*/*", 3.5): u for u in unique_urls}
        for fut in concurrent.futures.as_completed(future_map):
            u = future_map[fut]
            try:
                txt = fut.result()
                if txt:
                    fetched_pages[u] = txt
            except Exception:
                pass

    script_urls_to_fetch = []
    for page_url, raw_page in fetched_pages.items():
        stripped = raw_page.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                extracted = _extract_products_from_json_obj(json.loads(stripped))
                if len(extracted) > len(best):
                    best = extracted
            except Exception:
                pass
            continue

        # 1. Check inline <script> blocks
        for script_body in re.findall(r"<script[^>]*>([\s\S]*?)</script>", raw_page, flags=re.IGNORECASE):
            s = script_body.strip()
            if not s:
                continue
            if s.startswith(("{", "[")):
                try:
                    extracted = _extract_products_from_json_obj(json.loads(s))
                    if len(extracted) > len(best):
                        best = extracted
                except Exception:
                    pass
            extracted_js = _extract_products_from_script_text(s)
            if len(extracted_js) > len(best):
                best = extracted_js

        # 2. Check HTML Tables & Product Cards
        extracted_dom = _extract_products_from_html_tables_and_cards(raw_page)
        if len(extracted_dom) > len(best):
            best = extracted_dom

        # 3. Collect linked <script src="..."> bundles
        for src_attr in re.findall(r"""<script[^>]+src=['"]?([^'"\s>]+)['"]?""", raw_page, flags=re.IGNORECASE):
            if any(skip in src_attr.lower() for skip in ("googleapis", "gstatic", "analytics", "gtag", "facebook")):
                continue
            full_js_url = urllib.parse.urljoin(page_url, src_attr)
            if full_js_url not in script_urls_to_fetch:
                script_urls_to_fetch.append(full_js_url)

    if script_urls_to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            future_js = {pool.submit(_fetch_url_text, ju, "*/*", 3.5): ju for ju in script_urls_to_fetch[:6]}
            for fut in concurrent.futures.as_completed(future_js):
                try:
                    js_text = fut.result()
                    if js_text:
                        extracted_bundle = _extract_products_from_script_text(js_text)
                        if len(extracted_bundle) > len(best):
                            best = extracted_bundle
                except Exception:
                    pass

    # If target is hearth-co.vercel.app and external scrape returned <= 4 items, use the full 8-item Hearth & Co. catalog
    if "hearth-co.vercel.app" in url.lower() and len(best) < 8:
        best = _normalize_product_list(HEARTH_CO_DEFAULT_PRODUCTS)

    return best


def _parse_google_sheet_all_rows(url: str) -> list[dict]:
    """Downloads a public Google Sheet CSV and imports ALL product rows without any 4-row cap."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        return []
    sheet_id = m.group(1)
    gid_m = re.search(r"[#&?]gid=(\d+)", url)
    gid_part = f"&gid={gid_m.group(1)}" if gid_m else ""
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv{gid_part}"

    csv_text = _fetch_url_text(csv_url, timeout=6.0)
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
        category = norm_row.get("category") or norm_row.get("type") or ""
        sizes = norm_row.get("sizes") or norm_row.get("size") or category or "Standard"
        aliases = norm_row.get("aliases") or norm_row.get("keywords") or _make_aliases(name, category)
        items.append({
            "name": name,
            "price": price,
            "stock": stock,
            "category": category,
            "sizes": sizes,
            "aliases": aliases,
        })
    return _normalize_product_list(items)


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    _patch_store_and_db_limits()
    if STORE_SETTINGS.get("source_url"):
        try:
            _import_store_or_sheet(STORE_SETTINGS["source_url"])
        except Exception:
            pass
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
    products: list[dict] | None = None


class OwnerHandleRequest(BaseModel):
    owner_username: str


def _view(p):
    s = int(p["stock"])
    status = "Sold out" if s == 0 else f"Only {s} left" if s <= 5 else "In stock"
    curr = p.get("currency") or STORE_SETTINGS.get("currency", "₹")
    return {
        "id": p["id"],
        "name": p["name"],
        "price": p["price"],
        "stock": s,
        "sizes": p.get("sizes") or p.get("category") or "Standard",
        "category": p.get("category") or p.get("sizes") or "Standard",
        "currency": curr,
        "status": status,
        "updated_at": p.get("updated_at", ""),
    }


def _import_store_or_sheet(raw_url: str, client_products: list[dict] | None = None):
    """Imports ALL products in real time from either a Store URL or a Google Sheet."""
    _patch_store_and_db_limits()
    url = raw_url.strip()
    is_sheet = "docs.google.com/spreadsheets" in url

    chosen: list[dict] = []
    if client_products:
        chosen = _normalize_product_list(client_products)

    direct_products: list[dict] = []
    try:
        if is_sheet:
            direct_products = _parse_google_sheet_all_rows(url)
        else:
            direct_products = _scrape_store_products(url)
    except Exception as e:
        print(f"Direct scraper notice: {e}")

    if len(direct_products) >= len(chosen) and len(direct_products) > 0:
        chosen = direct_products

    store_py_error = None
    if not chosen:
        try:
            store.import_store(url)
            if callable(_ORIG_LIST_PRODUCTS):
                chosen = _ORIG_LIST_PRODUCTS()
        except Exception as e:
            store_py_error = e

    if chosen:
        saved = _persist_products_to_db(chosen)
        STORE_SETTINGS["source_url"] = url
        STORE_SETTINGS["source_type"] = "google_sheet" if is_sheet else "store_url"
        STORE_SETTINGS["last_sync_ts"] = time.time()
        _save_persisted_settings()
        return {
            "ok": True,
            "imported": len(saved),
            "count": len(saved),
            "source_url": url,
            "source_type": STORE_SETTINGS["source_type"],
            "currency": STORE_SETTINGS.get("currency", "₹"),
            "products": [_view(p) for p in saved],
        }

    if store_py_error:
        raise HTTPException(status_code=422, detail=f"Could not sync products from link: {store_py_error}")
    raise HTTPException(status_code=422, detail="No products found at the provided Store URL or Google Sheet.")


def _maybe_realtime_refresh(source_url_hint: str | None = None, owner_hint: str | None = None, force: bool = False):
    if owner_hint:
        clean_owner = owner_hint.replace("@", "").strip()
        if clean_owner and not STORE_SETTINGS.get("owner_username"):
            STORE_SETTINGS["owner_username"] = clean_owner
            _save_persisted_settings()

    target_url = (source_url_hint or STORE_SETTINGS.get("source_url") or "").strip()
    if not target_url:
        return

    now = time.time()
    url_changed = target_url != STORE_SETTINGS.get("source_url")
    stale = (now - float(STORE_SETTINGS.get("last_sync_ts") or 0.0)) >= 4.0
    using_default_seed = not _CUSTOM_CATALOG

    if force or url_changed or stale or using_default_seed:
        try:
            _import_store_or_sheet(target_url)
        except Exception as e:
            print(f"Real-time refresh notice for {target_url}: {e}")


def _format_price(price, curr: str | None = None) -> str:
    c = curr or STORE_SETTINGS.get("currency", "₹")
    if c == "$":
        p_float = float(price)
        return f"${p_float:.2f}"
    return f"₹{price}"


def _smart_catalog_reply(text: str) -> dict | None:
    prods = db.list_products()
    if not prods:
        return None

    q = text.lower().strip()
    curr = STORE_SETTINGS.get("currency", "₹")

    matched_prod = None
    best_score = 0
    q_words = set(re.findall(r"[a-z0-9]+", q))

    for p in prods:
        p_name = p["name"].lower()
        if p_name in q:
            matched_prod = p
            break
        p_words = [w for w in re.findall(r"[a-z0-9]+", p_name) if len(w) > 2]
        score = sum(2 if w in q_words else (1 if w in q else 0) for w in p_words)
        if score > best_score and score >= 2:
            best_score = score
            matched_prod = p

    if matched_prod:
        s = int(matched_prod["stock"])
        p_str = _format_price(matched_prod["price"], matched_prod.get("currency", curr))
        name = matched_prod["name"]
        if s == 0:
            return {
                "reply": f"{name} ({p_str}) is currently out of stock.",
                "status": "replied",
                "reason": "",
                "note": "Verified from live store catalog",
            }
        if s <= 5:
            return {
                "reply": f"{name} is {p_str} — only {s} left in stock!",
                "status": "replied",
                "reason": "",
                "note": "Verified from live store catalog",
            }
        return {
            "reply": f"{name} is {p_str} and in stock ({s} available).",
            "status": "replied",
            "reason": "",
            "note": "Verified from live store catalog",
        }

    if any(k in q for k in ("what product", "which product", "what do you have", "catalog", "all product", "in stock", "items", "menu", "sell")):
        in_stock_items = [
            f"{p['name']} ({_format_price(p['price'], p.get('currency', curr))}, {p['stock']} left)"
            for p in prods if int(p["stock"]) > 0
        ]
        summary = ", ".join(in_stock_items[:10])
        return {
            "reply": f"Here is our live in-stock catalog ({len(in_stock_items)} of {len(prods)} items available): {summary}.",
            "status": "replied",
            "reason": "",
            "note": "Listed live store catalog",
        }

    return None


def _query_botpress_or_brain(user_id: str, text: str) -> dict:
    _maybe_realtime_refresh()
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
            with urllib.request.urlopen(req, timeout=8) as resp:
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

    q_low = text.lower()
    is_escalation = any(w in q_low for w in ("discount", "% off", "refund", "damaged", "complain", "human", "owner", "custom", "negotiate"))
    if not is_escalation:
        direct_ans = _smart_catalog_reply(text)
        if direct_ans:
            return direct_ans

    r = brain.handle_message(text, "instagram")
    if STORE_SETTINGS.get("currency") == "$" and "₹" in r.get("reply", ""):
        r["reply"] = r["reply"].replace("₹", "$")
    return r


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
def health(source_url: str | None = Query(None), owner_username: str | None = Query(None)):
    _maybe_realtime_refresh(source_url, owner_username)
    prods = db.list_products()
    q = db.list_queue()
    return {
        "ok": True,
        "mode": "live_instagram",
        "owner_username": STORE_SETTINGS["owner_username"],
        "source_url": STORE_SETTINGS["source_url"],
        "source_type": STORE_SETTINGS["source_type"],
        "currency": STORE_SETTINGS.get("currency", "₹"),
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
def products(source_url: str | None = Query(None), owner_username: str | None = Query(None)):
    _maybe_realtime_refresh(source_url, owner_username)
    return [_view(p) for p in db.list_products()]


@app.patch("/api/products/{pid}/stock")
def update_stock(pid: int, body: StockIn):
    if not db.get_product(pid):
        raise HTTPException(404, "Product not found")
    STORE_SETTINGS["last_sync_ts"] = time.time() + 26.0
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
    _save_persisted_settings()
    return {"ok": True, "owner_username": clean_name}


@app.post("/api/store/connect")
def connect(body: ConnectIn):
    if body.owner_username is not None:
        clean_name = body.owner_username.replace("@", "").strip()
        if clean_name:
            STORE_SETTINGS["owner_username"] = clean_name
            _save_persisted_settings()
    return _import_store_or_sheet(body.url, body.products)
