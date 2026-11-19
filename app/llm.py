"""Gemini first, Groq as backup. Returns None if no key works. Stdlib only."""
import json, os, urllib.request

_cache = {}

def available():
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY"))

def _post(url, body, headers, timeout=8):
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def _gemini(prompt, system):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 200}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    d = _post(url, body, {"Content-Type": "application/json"})
    return d["candidates"][0]["content"]["parts"][0]["text"].strip()

def _groq(prompt, system):
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"), "messages": msgs,
            "temperature": 0.2, "max_tokens": 200}
    d = _post("https://api.groq.com/openai/v1/chat/completions", body,
              {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    return d["choices"][0]["message"]["content"].strip()

def call_llm(prompt, system=None):
    k = (system or "", prompt)
    if k in _cache:
        return _cache[k]
    for fn in (_gemini, _groq):
        try:
            out = fn(prompt, system)
            if out:
                if len(_cache) > 500:
                    _cache.clear()
                _cache[k] = out
                return out
        except Exception:
            continue
    return None
