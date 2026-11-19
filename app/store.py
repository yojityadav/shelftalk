"""Connect a store: Google Sheet / CSV link or a product page with JSON-LD. Stdlib only."""
import csv, io, ipaddress, json, re, socket, urllib.parse, urllib.request
from . import db

class StoreError(Exception):
    pass

def _check_url(url):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or "." not in u.hostname:
        raise StoreError("Enter a full link starting with https://")
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except OSError:
        raise StoreError("We could not find that website. Check the link.")
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise StoreError("That address is not allowed.")

def _fetch(url, limit=2_000_000):
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "ShelfTalk/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read(limit).decode("utf-8", "replace")
    except Exception:
        raise StoreError("We could not open that link. Is it public?")

def sheet_csv_url(url):
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if not m:
        return url
    gid = re.search(r"[#&?]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")

def _num(s):
    s = re.sub(r"[^\d.]", "", str(s or ""))
    return int(float(s)) if s else None

def parse_csv(text):
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        price, stock = _num(r.get("price")), _num(r.get("stock"))
        if r.get("name") and price is not None and stock is not None:
            rows.append({"name": r["name"], "price": price, "stock": stock,
                         "aliases": r.get("aliases", ""), "sizes": r.get("sizes", "")})
    return rows

def parse_jsonld(html):
    found = []
    def walk(o):
        if isinstance(o, list):
            for x in o: walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                off = o.get("offers") or {}
                off = off[0] if isinstance(off, list) and off else off
                price = _num(off.get("price")) if isinstance(off, dict) else None
                avail = str(off.get("availability", "")) if isinstance(off, dict) else ""
                if o.get("name") and price is not None:
                    found.append({"name": str(o["name"]), "price": price,
                                  "stock": 0 if "OutOfStock" in avail else 10, "aliases": "", "sizes": ""})
            for v in o.values(): walk(v)
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try: walk(json.loads(blob))
        except ValueError: pass
    return found

def import_store(url):
    url = (url or "").strip()
    text = _fetch(sheet_csv_url(url))
    warning = None
    if "<html" in text[:500].lower() or "application/ld+json" in text:
        rows = parse_jsonld(text)
        if rows:
            warning = "This page shows only in stock or out of stock, so in-stock items were set to 10. Edit stock in the owner screen."
    else:
        rows = parse_csv(text)
    if not rows:
        raise StoreError("No products found. A sheet needs the columns: name, price, stock.")
    for r in rows:
        db.upsert_product(r["name"], r["price"], r["stock"], r["aliases"], r["sizes"])
    return {"status": "ok", "products_found": len(rows), "warning": warning}
