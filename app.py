import hashlib
import html
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CACHE_TTL = 300  # seconds
VALID_COUNTS = {5, 10, 15, 20, 25, 30}
HDR = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

SOURCES = {
    "Business Standard": {
        "bias": 1.0,
        "feeds": [
            "https://www.business-standard.com/rss/markets-106.rss",
            "https://www.business-standard.com/rss/economy-102.rss",
        ],
        "gnews": "business-standard.com",
    },
    "SCMP": {
        "bias": 0.0,
        "feeds": [
            "https://www.scmp.com/rss/92/feed",
            "https://www.scmp.com/rss/91/feed",
        ],
        "gnews": "scmp.com",
    },
    "Al Jazeera": {
        "bias": 0.0,
        "feeds": ["https://www.aljazeera.com/xml/rss/all.xml"],
        "gnews": "aljazeera.com",
    },
}

POS = [
    "rally", "surge", "gain", "rise", "rises", "jump", "record high", "upgrade", "growth", "beat",
    "rate cut", "cuts rate", "easing", "eases", "inflow", "recovery", "recover", "strong", "boost",
    "optimism", "ceasefire", "truce", "deal", "stimulus", "soar", "rebound", "expansion", "upbeat",
    "cooling", "outperform", "rebounds", "advance", "climb", "bullish", "relief", "agreement",
]
NEG = [
    "fall", "falls", "plunge", "slump", "crash", "drop", "tumble", "selloff", "sell-off", "slowdown",
    "recession", "inflation", "rate hike", "tariff", "sanction", "war", "conflict", "strike", "attack",
    "outflow", "downgrade", "weak", "fear", "crisis", "default", "deficit", "shutdown", "escalat",
    "tension", "loss", "slide", "warn", "risk", "ban", "missile", "bomb", "bearish", "volatil",
    "retreat", "decline", "shrink", "layoff", "probe", "fraud",
]
POS_RE = [re.compile(r"\b" + re.escape(k)) for k in POS]
NEG_RE = [re.compile(r"\b" + re.escape(k)) for k in NEG]

# (keywords, tags, weight, invert, positive India line, negative India line)
THEMES = [
    (["crude", "brent", "opec", "oil price", "oil prices", "oil "], {"economy", "market", "global"}, 3.5, True,
     "Softer crude eases India's import bill, inflation and current-account pressure.",
     "Costlier crude widens India's import bill and adds to inflation and rupee pressure."),
    (["rbi", "repo", "monetary policy", "mpc", "reserve bank"], {"economy", "market"}, 4.0, False,
     "Supportive RBI stance helps credit growth and rate-sensitive sectors.",
     "Tighter RBI stance raises borrowing costs and weighs on rate-sensitive stocks."),
    (["sensex", "nifty", "dalal street", "stock market", "equities", "bse", "nse", "fii", "fpi", "ipo"], {"market"}, 4.0, False,
     "Firmer domestic equity mood supports Nifty and Sensex sentiment.",
     "Weak equity mood pressures Nifty and Sensex and could prompt foreign outflows."),
    (["rupee", "forex", "dollar index", "currency"], {"economy", "market"}, 3.0, False,
     "A steadier rupee lowers imported inflation and supports foreign inflows.",
     "A weaker rupee lifts import costs and risks foreign portfolio outflows."),
    (["gdp", "pmi", "iip", "cpi", "wpi", "fiscal", "gst", "budget", "exports", "imports", "manufacturing", "services"], {"economy"}, 3.0, False,
     "Healthy macro data reinforces India's growth and demand outlook.",
     "Soft macro readings dent India's growth and demand outlook."),
    (["fed ", "federal reserve", "powell", "wall street", "nasdaq", "dow ", "s&p", "ecb", "boj", "treasury yield"], {"global", "market"}, 3.0, False,
     "Benign global rates and risk appetite favour emerging-market flows into India.",
     "Tighter global liquidity or risk-off mood tends to pull money out of Indian assets."),
    (["china", "hong kong", "taiwan", "trade war", "tariff", "trump", "export curbs"], {"global", "economy"}, 2.5, False,
     "Calmer China and trade ties help Indian exporters and supply chains.",
     "China and trade friction can disrupt supply chains and hit Indian exporters."),
    (["iran", "israel", "gaza", "russia", "ukraine", "red sea", "hormuz", "houthi", "middle east", "gulf"], {"global", "economy", "market"}, 3.0, False,
     "De-escalation in the Gulf and Europe cools oil and risk premiums for India.",
     "Geopolitical escalation lifts oil and shipping costs and drives risk-off for India."),
    (["gold", "silver", "commodity", "commodities", "metal", "steel"], {"economy", "market"}, 1.5, False,
     "Commodity moves are favourable for India's trade balance and metal producers.",
     "Commodity swings add input-cost and trade-balance pressure for India."),
]
NEUTRAL_LINE = "Limited direct read-through for India; watch for follow-on moves in oil, rupee and flows."

_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def clean(text):
    text = html.unescape(text or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def parse_date(s):
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def fetch_feed(url, source, strip_suffix=False):
    r = requests.get(url, headers=HDR, timeout=8)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        title = clean(it.findtext("title"))
        if strip_suffix and " - " in title:
            title = title.rsplit(" - ", 1)[0]
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        out.append({
            "title": title,
            "link": link,
            "summary": clean(it.findtext("description"))[:400] if not strip_suffix else "",
            "published": parse_date(it.findtext("pubDate") or ""),
            "source": source,
        })
    return out


def fetch_source(name, cfg):
    items = []
    for url in cfg["feeds"]:
        try:
            items += fetch_feed(url, name)
        except Exception:
            continue
    if not items:  # fallback: Google News RSS restricted to the site
        try:
            q = quote(f"site:{cfg['gnews']} when:1d")
            items = fetch_feed(
                f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en",
                name, strip_suffix=True,
            )
        except Exception:
            items = []
    return name, items


def count_hits(regexes, text):
    return sum(len(r.findall(text)) for r in regexes)


def score(item):
    title = item["title"].lower() + " "
    body = (item["summary"] or "").lower()
    raw = 2 * (count_hits(POS_RE, title) - count_hits(NEG_RE, title)) + (count_hits(POS_RE, body) - count_hits(NEG_RE, body))
    sent = math.tanh(raw / 3.0)
    text = title + body

    matched = [t for t in THEMES if any(k in text for k in t[0])]
    matched.sort(key=lambda t: t[2], reverse=True)
    tags, weight, line = set(), 0.0, NEUTRAL_LINE
    if matched:
        primary = matched[0]
        tags = set().union(*(t[1] for t in matched))
        weight = sum(t[2] for t in matched[:3])
        if primary[3]:
            sent = -sent
        if sent > 0.15:
            line = primary[4]
        elif sent < -0.15:
            line = primary[5]
    age_h = max((datetime.now(timezone.utc) - item["published"]).total_seconds() / 3600, 0)
    recency = 2.0 * math.exp(-age_h / 18.0)
    impact = weight * (1 + abs(sent)) + recency + SOURCES[item["source"]]["bias"]

    prob = int(round(50 + 45 * sent))
    label = "bullish" if prob >= 58 else "bearish" if prob <= 42 else "neutral"
    return {
        "id": hashlib.md5(item["link"].encode()).hexdigest()[:12],
        "headline": item["title"],
        "link": item["link"],
        "source": item["source"],
        "published": item["published"].isoformat(),
        "impact_line": line,
        "sentiment": label,
        "prob": prob,
        "_sent": sent,
        "_impact": impact,
        "_tags": tags,
    }


def gauge(items, tag):
    rel = [i for i in items if tag in i["_tags"]]
    if not rel:
        return {"prob": 50, "label": "neutral", "n": 0}
    w = sum(i["_impact"] for i in rel)
    avg = sum(i["_sent"] * i["_impact"] for i in rel) / w if w else 0
    prob = int(round(50 + 45 * math.tanh(avg * 1.5)))
    label = "bullish" if prob >= 58 else "bearish" if prob <= 42 else "neutral"
    return {"prob": prob, "label": label, "n": len(rel)}


def build():
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(lambda kv: fetch_source(*kv), SOURCES.items()))
    ok = [name for name, items in results if items]
    seen, scored = set(), []
    for _, items in results:
        for it in items:
            key = re.sub(r"\W+", "", it["title"].lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            scored.append(score(it))
    scored.sort(key=lambda i: i["_impact"], reverse=True)
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "sources_ok": ok,
        "items": scored,
        "gauges": {
            "economy": gauge(scored, "economy"),
            "market": gauge(scored, "market"),
            "global": gauge(scored, "global"),
        },
    }


def get_data():
    with _lock:
        if _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["data"]
        try:
            data = build()
            if data["items"]:
                _cache.update(ts=time.time(), data=data)
                return data
        except Exception:
            pass
        return _cache["data"]  # stale copy or None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/news")
def api_news():
    try:
        count = int(request.args.get("count", 10))
    except ValueError:
        count = 10
    if count not in VALID_COUNTS:
        count = 10
    data = get_data()
    if not data:
        return jsonify(error="Feeds unavailable. Retry shortly."), 503
    slides = [{k: v for k, v in i.items() if not k.startswith("_")} for i in data["items"][:count]]
    return jsonify(updated=data["updated"], sources_ok=data["sources_ok"],
                   gauges=data["gauges"], count=count, slides=slides)


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
