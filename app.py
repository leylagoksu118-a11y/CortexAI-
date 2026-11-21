# Cortex AI araç sunucusu: DuckDuckGo arama, Vikipedi, BeautifulSoup ile sayfa okuma.
import os, re, time, socket, ipaddress
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=None)
HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED = os.environ.get("ALLOWED_ORIGIN", "")  # arayüz başka adresteyse (örn. Netlify) onun adresi
UA = {"User-Agent": "Mozilla/5.0 (compatible; CortexAI/1.0)"}
HITS = {}

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = ALLOWED or "*"
    r.headers["Access-Control-Allow-Headers"] = "content-type"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return r

@app.before_request
def limit():
    if request.method == "OPTIONS" or not request.path.startswith("/api/"):
        return
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    a = [t for t in HITS.get(ip, []) if now - t < 60] + [now]
    HITS[ip] = a
    if len(a) > 30:
        return jsonify(error="Çok fazla istek, biraz bekle."), 429

@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")

@app.get("/api/health")
def health():
    return jsonify(ok=True, ai=bool(AI_KEY))

@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip()[:200]
    if not q:
        return jsonify(error="Boş arama"), 400
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        res = DDGS().text(q, region="tr-tr", max_results=5)
        return jsonify(results=[{"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")} for r in res])
    except Exception as e:
        return jsonify(error=f"Arama başarısız: {e}"), 502

@app.get("/api/wiki")
def wiki():
    q = request.args.get("q", "").strip()[:200]
    lang = request.args.get("lang", "tr")
    if not q:
        return jsonify(error="Boş arama"), 400
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        lang = "tr"
    try:
        r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", headers=UA, timeout=10, params={
            "action": "query", "generator": "search", "gsrsearch": q, "gsrlimit": 1, "prop": "extracts|info",
            "inprop": "url", "explaintext": 1, "exchars": 6000, "format": "json", "redirects": 1})
        pages = (r.json().get("query") or {}).get("pages") or {}
        if not pages:
            return jsonify(found=False)
        p = next(iter(pages.values()))
        return jsonify(found=True, title=p.get("title"), url=p.get("fullurl"), text=p.get("extract", ""))
    except Exception as e:
        return jsonify(error=f"Vikipedi hatası: {e}"), 502

def safe(url):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)  # iç ağ adreslerini engeller

@app.get("/api/scrape")
def scrape():
    url = request.args.get("url", "").strip()
    try:
        for _ in range(4):
            if not safe(url):
                return jsonify(error="Bu adres açılamaz."), 400
            r = requests.get(url, headers=UA, timeout=10, stream=True, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, r.headers.get("Location", ""))
                continue
            break
        else:
            return jsonify(error="Çok fazla yönlendirme."), 400
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            return jsonify(error="Sadece web sayfaları okunabilir."), 415
        soup = BeautifulSoup(r.raw.read(2_000_000, decode_content=True), "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
            t.decompose()
        title = (soup.title.string or "").strip() if soup.title else ""
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:8000]
        return jsonify(url=url, title=title or url, text=text)
    except Exception as e:
        return jsonify(error=f"Sayfa okunamadı: {e}"), 502


# --- Sohbet: anahtar SADECE sunucuda durur, ziyaretçi hiç görmez ---
AI_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1")
MODELS = [m.strip() for m in os.environ.get("AI_MODEL", "llama-3.3-70b-versatile,llama-3.1-8b-instant").split(",") if m.strip()]
VISION = os.environ.get("VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

@app.post("/api/chat")
def chat():
    if not AI_KEY:
        return jsonify(error="Yapay zekâ henüz ayarlanmamış (sunucuda AI_API_KEY yok)."), 503
    b = request.get_json(silent=True) or {}
    msgs = [{"role": m["role"], "content": m["content"][:30000]} for m in (b.get("messages") or [])[-20:]
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    if not msgs or msgs[-1]["role"] != "user":
        return jsonify(error="Geçersiz mesaj."), 400
    system = str(b.get("system") or "")[:6000]
    imgs = [i for i in (b.get("images") or [])[:3] if isinstance(i, dict)
            and re.fullmatch(r"image/(jpeg|png|webp)", str(i.get("mime"))) and re.fullmatch(r"[A-Za-z0-9+/=]+", str(i.get("b64")))]
    models = MODELS
    if imgs:
        msgs[-1]["content"] = [{"type": "text", "text": msgs[-1]["content"]}] + [
            {"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['b64']}"}} for i in imgs]
        models = [VISION]
    full = ([{"role": "system", "content": system}] if system else []) + msgs
    r = None
    for m in models:
        try:
            r = requests.post(AI_BASE + "/chat/completions", headers={"Authorization": "Bearer " + AI_KEY}, timeout=60,
                              json={"model": m, "messages": full, "max_tokens": 4096 if b.get("svg") else 2048})
        except requests.RequestException:
            return jsonify(error="Yapay zekâ servisine ulaşılamadı."), 502
        if r.status_code not in (404, 429, 503):
            break
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code == 429:
        return jsonify(error="Şu an çok yoğun, birkaç saniye sonra tekrar dene."), 429
    if not r.ok:
        return jsonify(error=(j.get("error") or {}).get("message", "Yapay zekâ servisi hata verdi.")), 502
    return jsonify(text=j["choices"][0]["message"]["content"] or "")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
