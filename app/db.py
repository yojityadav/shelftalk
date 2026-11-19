"""SQLite data layer: products, chat log, owner queue. Stdlib only."""
import os, sqlite3, time

def _path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.getenv("DB_PATH") or os.path.join(root, "data", "shelftalk.db")

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def q(sql, args=(), commit=False):
    """Run one statement. Returns (rows as dicts, lastrowid)."""
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        last = cur.lastrowid
        if commit:
            c.commit()
        return rows, last
    finally:
        c.close()

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
        aliases TEXT DEFAULT '', price INTEGER NOT NULL, stock INTEGER NOT NULL,
        sizes TEXT DEFAULT '', updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS chat_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, source TEXT,
        customer_text TEXT, reply TEXT, status TEXT, reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS needs_owner(
        id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, customer_text TEXT,
        reason TEXT, resolved INTEGER DEFAULT 0)""",
]
SEED = [
    ("Silver Hoop Earrings", "hoop,hoops,earring,earrings,silver", 799, 12, ""),
    ("Linen Kurta", "kurta,linen", 1299, 2, "S, M, L, XL"),
    ("Canvas Tote Bag", "tote,bag,canvas", 649, 8, ""),
    ("Shea Lip Balm", "lip,balm,shea", 349, 0, ""),
]

def init_db():
    for s in SCHEMA:
        q(s, commit=True)
    if not q("SELECT 1 FROM products LIMIT 1")[0]:
        for n, a, p, s, z in SEED:
            q("INSERT INTO products(name,aliases,price,stock,sizes,updated_at) VALUES(?,?,?,?,?,?)",
              (n, a, p, s, z, now()), commit=True)

def list_products():
    return q("SELECT * FROM products ORDER BY id")[0]

def get_product(pid):
    r = q("SELECT * FROM products WHERE id=?", (pid,))[0]
    return r[0] if r else None

def set_stock(pid, stock):
    q("UPDATE products SET stock=?, updated_at=? WHERE id=?", (stock, now(), pid), commit=True)
    return get_product(pid)

def upsert_product(name, price, stock, aliases="", sizes=""):
    if not aliases:
        aliases = ",".join(w for w in name.lower().split() if len(w) > 2)
    hit = q("SELECT id FROM products WHERE lower(name)=lower(?)", (name,))[0]
    if hit:
        q("UPDATE products SET price=?, stock=?, sizes=COALESCE(NULLIF(?, ''), sizes), updated_at=? WHERE id=?",
          (price, stock, sizes, now(), hit[0]["id"]), commit=True)
    else:
        q("INSERT INTO products(name,aliases,price,stock,sizes,updated_at) VALUES(?,?,?,?,?,?)",
          (name, aliases, price, stock, sizes, now()), commit=True)

def log_chat(source, text, reply, status, reason):
    q("INSERT INTO chat_log(time,source,customer_text,reply,status,reason) VALUES(?,?,?,?,?,?)",
      (now(), source, text, reply, status, reason), commit=True)

def add_queue(text, reason):
    return q("INSERT INTO needs_owner(time,customer_text,reason) VALUES(?,?,?)",
             (now(), text, reason), commit=True)[1]

def list_queue():
    return q("SELECT id,time,customer_text,reason FROM needs_owner WHERE resolved=0 ORDER BY id DESC")[0]

def resolve_queue(qid):
    q("UPDATE needs_owner SET resolved=1 WHERE id=?", (qid,), commit=True)
    return bool(q("SELECT 1 FROM needs_owner WHERE id=? AND resolved=1", (qid,))[0])

def list_chats(limit=50):
    return q("SELECT * FROM chat_log ORDER BY id DESC LIMIT ?", (limit,))[0]
