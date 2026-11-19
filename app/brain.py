"""ShelfTalk brain: handle_message(text). Channel-agnostic. Stdlib only.

Pipeline per message:
 1. Rules catch handoff topics (human, complaint, refund, negotiation, custom).
 2. Code finds the product and intent; data is read live from SQLite / policy.txt.
 3. A reply is drafted from the data only. If an LLM key exists it may rewrite it
    in a friendlier tone, using only the same facts (or answer HANDOFF).
 4. Number check: every number in the final reply must appear in the facts.
    If not, hand off.
"""
import difflib, os, re
from . import db, llm

HANDOFF_MSG = "Thank you for your message. I have passed this to the owner, who will reply soon."
STOP = {"the", "and", "for", "you", "are", "how", "much", "this", "that", "with", "your", "have", "does", "what"}

HANDOFF_RULES = [  # (regex, reason, note shown in the owner queue)
    (r"\b(human|real person|someone|owner|agent|speak to|talk to)\b", "human_requested", "Needs a person"),
    (r"\b(refund|complain\w*|damag\w*|broken|wrong item|not received|fraud|scam)\b", "complaint", "Complaint or refund"),
    (r"(discount|\d+\s?% ?off|negotiat\w*|bargain|cheaper|best price)", "negotiation", "Wants a discount"),
    (r"\b(custom\w*|bulk|wholesale|personali[sz]ed)\b", "custom_order", "Custom order"),
]
GREETING = re.compile(r"(hi+|hello+|hey+|namaste|good (morning|afternoon|evening))[ !.]*")


# ---------- policy (read fresh every call) ----------
def load_policy():
    path = os.getenv("POLICY_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "policy.txt")
    pol = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    k, v = line.split(":", 1)
                    pol[k.strip().lower()] = v.strip()
    except OSError:
        pass
    pol["cities"] = [c.strip().lower() for c in pol.get("delivery_cities", "").split(",") if c.strip()]
    return pol


# ---------- helpers ----------
def fmt(n):
    return "\u20b9" + f"{n:,}"

def _tokens(text):
    toks = set(re.findall(r"[a-z0-9]+", text.lower()))
    return toks | {t[:-1] for t in toks if t.endswith("s") and len(t) > 3}

def match_products(text, products):
    """Return the best-matching products (several = ambiguous, none = no match)."""
    toks = _tokens(text)
    long_toks = [t for t in toks if len(t) >= 4]
    scored = []
    for p in products:
        words = set(re.findall(r"[a-z0-9]+", p["name"].lower()))
        for a in p["aliases"].split(","):
            words |= set(re.findall(r"[a-z0-9]+", a.lower()))
        words = {w for w in words if len(w) >= 3 and w not in STOP}
        hits = 0
        for w in words:
            if w in toks:
                hits += 1
            elif len(w) >= 5 and difflib.get_close_matches(w, long_toks, n=1, cutoff=0.82):
                hits += 1
        scored.append((hits, p))
    best = max((h for h, _ in scored), default=0)
    return [p for h, p in scored if h == best] if best else []

def detect_intent(low):
    if re.search(r"\b(return|exchange)\w*", low): return "return"
    if re.search(r"\b(deliver\w*|ship\w*|courier|dispatch)\b", low): return "delivery"
    if re.search(r"\bsizes?\b", low): return "size"
    if re.search(r"\b(stock|available|availability|have|left)\b", low): return "stock"
    if re.search(r"(price|cost|how much|\brs\b|\u20b9)", low): return "price"
    return None

def _llm_intent(text):
    out = llm.call_llm(
        f'Customer message: "{text}"\nReturn ONLY JSON like {{"intent":"price|stock|size|delivery|return|other"}}.',
        system="You classify customer messages for a small store. Output JSON only.")
    if not out:
        return None
    m = re.search(r'"intent"\s*:\s*"(\w+)"', out)
    return m.group(1) if m and m.group(1) in {"price", "stock", "size", "delivery", "return"} else None

def number_check(reply, facts):
    """Every number in the reply must appear in the facts text."""
    norm = lambda s: re.sub(r"[,\u20b9]", "", s)
    allowed = {norm(n) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", facts)}
    return all(norm(n) in allowed for n in re.findall(r"\d[\d,]*(?:\.\d+)?", reply))

def _res(reply, status, reason, data=None, note=None):
    return {"reply": reply, "status": status, "reason": reason, "data_used": data, "note": note}

def _handoff(reason, note, reply=HANDOFF_MSG, data=None):
    return _res(reply, "handoff", reason, data, note)

def _finalize(draft, facts, data, intent):
    """Optional friendly rewrite by the LLM, then the number check."""
    reply = draft
    if llm.available():
        out = llm.call_llm(
            f"Facts: {facts}\nDraft reply: {draft}\n"
            "Rewrite the draft as a short, friendly reply (max 2 sentences). Use ONLY the facts. "
            "Do not add any number that is not in the facts. If the facts cannot answer, output exactly HANDOFF.",
            system="You write replies for a small online store's DMs.")
        if out:
            if out.strip().upper() == "HANDOFF":
                return _handoff("llm_handoff", "Needs a person")
            reply = out.strip()
    if not number_check(reply, facts):
        return _handoff("number_check_failed", "Reply failed the number check", data=data)
    return _res(reply, "answered", intent, data)


# ---------- main brain ----------
def answer(text):
    low = text.lower().strip()
    if GREETING.fullmatch(low):
        return _res("Hi! Ask me about price, stock, size, delivery or returns.", "answered", "greeting")
    for rx, reason, note in HANDOFF_RULES:
        if re.search(rx, low):
            return _handoff(reason, note)

    products = db.list_products()
    matches = match_products(low, products)
    intent = detect_intent(low)
    pol = load_policy()

    if intent == "return":
        if not pol.get("return_policy"):
            return _handoff("no_policy", "No return policy on file")
        return _finalize(pol["return_policy"], pol["return_policy"], {"policy": "return"}, "return")

    if intent == "delivery":
        city = next((c for c in pol["cities"] if re.search(rf"\b{re.escape(c)}\b", low)), None)
        if city:
            days = pol.get("delivery_days", "")
            if not days:
                return _handoff("no_policy", "No delivery times on file")
            facts = f"Delivery to {city.title()} takes {days} days."
            return _finalize(f"Yes, we deliver to {city.title()} in {days} days.", facts,
                             {"city": city, "days": days}, "delivery")
        asked = re.search(r"\b(?:deliver\w*|ship\w*)\s+(?:to|in)\s+([a-z]+(?: [a-z]+)?)", low)
        if asked and asked.group(1).split()[0] not in {"my", "me", "the", "your"}:
            return _handoff("city_not_covered", f"City not in delivery list: {asked.group(1).title()}",
                            reply="I have asked the owner to confirm delivery to your city.")
        return _res("Which city should we deliver to?", "answered", "clarify_city")

    if len(matches) > 1:
        names = ", ".join(p["name"] for p in matches[:3])
        return _res(f"Which one do you mean: {names}?", "answered", "clarify_product")

    if not matches:
        return _handoff("no_match", "No matching product or policy",
                        reply="I am not sure about that, so I have passed it to the owner.")

    p = matches[0]
    data = {"product": p["name"], "price": p["price"], "stock": p["stock"]}
    if p["stock"] == 0:
        facts = f"{p['name']} is sold out. Stock 0."
        return _finalize(f"{p['name']}: sold out at the moment.", facts, data, "sold_out")
    if p["stock"] <= 2:
        return _handoff("low_stock", f"Low stock: {p['name']}",
                        reply="Only a few left. The owner will confirm availability.", data=data)
    facts = f"{p['name']} costs {fmt(p['price'])} ({p['price']}). In stock: {p['stock']} units."
    if intent == "size":
        if not p["sizes"]:
            return _handoff("no_data", f"No size data: {p['name']}")
        facts += f" Sizes: {p['sizes']}."
        return _finalize(f"{p['name']} sizes: {p['sizes']}.", facts, data, "size")
    if intent == "stock":
        return _finalize(f"{p['name']}: in stock.", facts, data, "stock")
    return _finalize(f"{p['name']}: {fmt(p['price'])}, in stock.", facts, data, "price")


def handle_message(text, source="web"):
    """The one function every channel calls. Logs the exchange and queues handoffs."""
    text = (text or "").strip()
    if not text:
        return _res("Please type your question.", "answered", "empty")
    text = text[:500]
    r = answer(text)
    db.log_chat(source, text, r["reply"], r["status"], r["reason"])
    if r["status"] == "handoff":
        r["queue_id"] = db.add_queue(text, r["note"] or r["reason"])
    return r
