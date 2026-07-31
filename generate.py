#!/usr/bin/env python3
"""Daily Market Brief generator.

Fetches news from Google News RSS feeds, summarizes/rates the top stories
(Google Gemini API when GEMINI_API_KEY is set, heuristic fallback otherwise),
fetches market indicators via yfinance (+ FRED for the US 2Y yield), and
renders static HTML pages into docs/ from a Jinja2 template.

Designed to be run daily by GitHub Actions (9:00 AM Hong Kong time).
Exit code is 0 on success even if individual sources fail; non-zero only on
unrecoverable errors (e.g. template rendering / writing output fails).
"""

import calendar
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("market-brief")

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "docs"
DAYS_DIR = DOCS_DIR / "days"
DATA_DIR = DOCS_DIR / "data"
TEMPLATE_PATH = ROOT / "templates" / "dashboard.html.j2"

HK_TZ = ZoneInfo("Asia/Hong_Kong")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
# "vertex" | "aistudio" | "" (auto-detect from the key format)
GEMINI_PROVIDER = os.environ.get("GEMINI_PROVIDER", "").strip().lower()

HTTP_TIMEOUT = 20


def gemini_provider():
    """Which Gemini endpoint to use. Vertex AI express-mode keys start with
    "AQ."; classic AI Studio keys start with "AIza". GEMINI_PROVIDER env var
    overrides auto-detection."""
    if GEMINI_PROVIDER in ("vertex", "aistudio"):
        return GEMINI_PROVIDER
    if GEMINI_API_KEY.startswith("AQ."):
        return "vertex"
    return "aistudio"


def gemini_generate(payload):
    """POST a generateContent request to the appropriate Gemini endpoint.
    Always sends contents with an explicit role (Vertex rejects role-less
    requests; AI Studio accepts them). Raises on any failure."""
    provider = gemini_provider()
    if provider == "vertex":
        url = ("https://aiplatform.googleapis.com/v1/publishers/google/models/"
               f"{GEMINI_MODEL}:generateContent")
        headers = {"x-goog-api-key": GEMINI_API_KEY}
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
    else:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
        resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# News categories
# ---------------------------------------------------------------------------

CATEGORIES = [
    {"id": "global-macro", "label": "Global Economy",
     "query": "global economy OR central bank OR inflation"},
    {"id": "equity", "label": "Equity Markets",
     "query": "stock market OR equities OR earnings"},
    {"id": "fixed-income", "label": "Fixed Income",
     "query": "bond yields OR treasuries OR credit markets"},
    {"id": "china", "label": "China Economy",
     "query": "China economy OR PBoC OR China GDP"},
    {"id": "hk", "label": "Hong Kong Economy",
     "query": "Hong Kong economy OR HKEX OR Hong Kong property"},
    {"id": "tech", "label": "Global Technology",
     "query": "big tech OR technology industry"},
    {"id": "ai", "label": "Artificial Intelligence",
     "query": "artificial intelligence OR OpenAI OR LLM"},
    {"id": "semis", "label": "Semiconductors",
     "query": "semiconductors OR TSMC OR NVIDIA OR chips"},
    {"id": "ev", "label": "Electric Vehicles",
     "query": "electric vehicles OR Tesla OR BYD OR EV sales"},
    {"id": "data-center", "label": "Data Centers",
     "query": "data centers OR hyperscale OR AI infrastructure"},
    {"id": "space", "label": "Space & Aerospace",
     "query": "space industry OR SpaceX OR satellite OR rocket launch"},
]

# ---------------------------------------------------------------------------
# Daily key takeaways
# ---------------------------------------------------------------------------

TAKEAWAYS_PROMPT = """You are a financial news analyst writing the lead summary of a daily market brief.

Below are the day's selected top stories across all coverage categories, with their importance ratings and the key points digested from each article.

Write exactly 3-4 concise analyst-style bullet points synthesizing the day's most important cross-market findings — connect themes across categories where relevant. Synthesize insights from the key points; do NOT just list or restate headlines.

Return STRICT JSON only: an object with one key "takeaways", an array of 3-4 bullet strings.

Top stories:
{items}
"""


def heuristic_takeaways(news):
    """Fallback: 3-4 highest-rated items across all categories (ties broken
    by category order), prefixed with the category label. Uses the best
    available text (digested bullet) rather than the bare title."""
    ranked = []
    for cat in news:
        for item in cat["items"]:
            text = ""
            for b in item.get("bullets", []):
                if b and b != "Summary not available.":
                    text = b
                    break
            if not text:
                text = item["title"]
            ranked.append((item["rating"], cat["label"], text))
    # Stable sort by rating desc keeps category order for ties.
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [f"[{label}] {text}" for _, label, text in ranked[:4]]


def gemini_takeaways(news):
    """One extra Gemini call for cross-market takeaways. Raises on failure."""
    lines = []
    for cat in news:
        for item in cat["items"]:
            bullets = " | ".join(item.get("bullets", []))
            lines.append(
                f"[{cat['label']}] (rating {item['rating']}/5) {item['title']}\n"
                f"    Key points: {bullets}"
            )
    prompt = TAKEAWAYS_PROMPT.format(items="\n".join(lines))
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }
    data = gemini_generate(payload)
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    takeaways = parsed.get("takeaways")
    if not isinstance(takeaways, list) or not takeaways:
        raise ValueError("Gemini takeaways missing or not a list")
    return [str(t) for t in takeaways][:4]


def build_takeaways(news):
    if GEMINI_API_KEY:
        try:
            log.info("Generating takeaways with Gemini")
            return gemini_takeaways(news)
        except Exception as exc:
            log.warning("Gemini takeaways failed (%s); using heuristic", exc)
    else:
        log.info("No GEMINI_API_KEY; heuristic takeaways")
    return heuristic_takeaways(news)


# ---------------------------------------------------------------------------
# Market indicators
# ---------------------------------------------------------------------------
# kind: "price" -> % change; "yield" -> bp change. yf=False -> FRED source.

INDICATOR_GROUPS = [
    {
        "name": "Equity Indices",
        "items": [
            {"label": "S&P 500", "ticker": "^GSPC", "kind": "price"},
            {"label": "Nasdaq", "ticker": "^IXIC", "kind": "price"},
            {"label": "Dow Jones", "ticker": "^DJI", "kind": "price"},
            {"label": "FTSE 100", "ticker": "^FTSE", "kind": "price"},
            {"label": "STOXX 600", "ticker": "^STOXX", "kind": "price"},
            {"label": "Nikkei 225", "ticker": "^N225", "kind": "price"},
            {"label": "Hang Seng", "ticker": "^HSI", "kind": "price"},
            {"label": "Hang Seng Tech", "ticker": "^HSTECH", "kind": "price"},
            {"label": "CSI 300", "ticker": "000300.SS", "kind": "price"},
        ],
    },
    {
        "name": "Bond Yields",
        "items": [
            {"label": "US 10Y", "ticker": "^TNX", "kind": "yield"},
            {"label": "US 30Y", "ticker": "^TYX", "kind": "yield"},
            {"label": "US 2Y", "ticker": "DGS2", "kind": "yield", "fred": True},
        ],
    },
    {
        "name": "FX",
        "items": [
            {"label": "US Dollar Index", "ticker": "DX-Y.NYB", "kind": "price"},
            {"label": "EUR/USD", "ticker": "EURUSD=X", "kind": "price"},
            {"label": "USD/JPY", "ticker": "JPY=X", "kind": "price"},
            {"label": "USD/CNH", "ticker": "CNH=X", "kind": "price"},
            {"label": "USD/HKD", "ticker": "HKD=X", "kind": "price"},
        ],
    },
    {
        "name": "Commodities",
        "items": [
            {"label": "Gold", "ticker": "GC=F", "kind": "price"},
            {"label": "WTI Crude", "ticker": "CL=F", "kind": "price"},
            {"label": "Brent", "ticker": "BZ=F", "kind": "price"},
            {"label": "Copper", "ticker": "HG=F", "kind": "price"},
        ],
    },
    {
        "name": "Volatility & Crypto",
        "items": [
            {"label": "VIX", "ticker": "^VIX", "kind": "price"},
            {"label": "Bitcoin", "ticker": "BTC-USD", "kind": "price"},
        ],
    },
]

SPARKLINE_POINTS = 22

# ---------------------------------------------------------------------------
# News fetching
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Bound the per-category extraction cost: only the top N candidates get full
# article text, each with a short timeout, fetched concurrently.
ARTICLE_EXTRACT_TOP_N = 8
ARTICLE_EXTRACT_WORKERS = 6
ARTICLE_TIMEOUT = 10


def strip_html(text):
    if not text:
        return ""
    return unescape(_TAG_RE.sub("", text)).strip()


def resolve_article_url(link):
    """Resolve a news.google.com/rss/articles URL to the publisher URL.

    Google News links no longer plain-redirect, so first try decoding via the
    googlenewsdecoder package; fall back to following HTTP redirects.
    Returns the publisher URL, or None on failure.
    """
    try:
        from googlenewsdecoder import gnewsdecoder
        result = gnewsdecoder(link)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
    except Exception:
        pass
    try:
        resp = requests.get(link, timeout=ARTICLE_TIMEOUT, allow_redirects=True,
                            headers={"User-Agent": BROWSER_UA})
        if resp.url and "news.google.com" not in urllib.parse.urlparse(resp.url).netloc:
            return resp.url
    except Exception:
        pass
    return None


def extract_article_text(link):
    """Resolve a (Google News) link and extract the main article text with
    trafilatura. Returns the text, or None on any failure (paywall, 404,
    JS-only page, timeout) — callers must degrade gracefully."""
    try:
        url = resolve_article_url(link)
        if not url:
            return None
        resp = requests.get(url, timeout=ARTICLE_TIMEOUT,
                            headers={"User-Agent": BROWSER_UA})
        if not resp.ok or not resp.content:
            return None
        import trafilatura
        # Pass raw bytes so trafilatura handles charset detection itself
        # (requests' guessed encoding can mangle UTF-8 pages).
        text = trafilatura.extract(resp.content)
        # Very short "extractions" are usually consent walls or nav junk.
        if text and len(text) > 200:
            return text
    except Exception:
        pass
    return None


def attach_article_texts(category, items):
    """Extract full article text for the top candidates, concurrently.
    Sets item['content'] (None when extraction failed)."""
    for item in items:
        item["content"] = None
    top = items[:ARTICLE_EXTRACT_TOP_N]
    if not top:
        return
    extracted = 0
    with ThreadPoolExecutor(max_workers=ARTICLE_EXTRACT_WORKERS) as pool:
        futures = {pool.submit(extract_article_text, i["link"]): i
                   for i in top if i["link"]}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                item["content"] = fut.result()
            except Exception:
                item["content"] = None
            if item["content"]:
                extracted += 1
    log.info("%s: extracted %d/%d articles",
             category["label"], extracted, len(futures))


def fetch_category_news(category):
    """Fetch up to 15 recent RSS items for a category (prefer last 48h)."""
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(category["query"])
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    log.info("Fetching RSS: %s", category["label"])
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:  # network/parse issues are non-fatal
        log.warning("RSS fetch failed for %s: %s", category["label"], exc)
        return []

    now = time.time()
    items = []
    for entry in feed.entries[:40]:
        published = ""
        ts = None
        parsed = getattr(entry, "published_parsed", None) or getattr(
            entry, "updated_parsed", None)
        if parsed:
            ts = calendar.timegm(parsed)  # feedparser gives UTC struct_time
            published = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")
        source = ""
        src = getattr(entry, "source", None)
        if src is not None and getattr(src, "title", None):
            source = src.title
        elif getattr(feed.feed, "title", None):
            source = feed.feed.title
        items.append({
            "title": strip_html(getattr(entry, "title", "")),
            "link": getattr(entry, "link", ""),
            "source": source,
            "published": published,
            "summary": strip_html(getattr(entry, "summary", "")),
            "_ts": ts or 0,
        })

    # Prefer items from the last 48 hours; fall back to whatever we have.
    recent = [i for i in items if i["_ts"] and now - i["_ts"] <= 48 * 3600]
    pool = recent if recent else items
    pool.sort(key=lambda i: i["_ts"], reverse=True)
    top = pool[:15]
    attach_article_texts(category, top)
    return top


# ---------------------------------------------------------------------------
# Summarization / rating
# ---------------------------------------------------------------------------

RATING_STRONG = ["crash", "plunge", "record", "fed", "rate cut", "default",
                 "crisis", "surge", "ban", "tariff", "sanction", "bailout",
                 "recession"]
RATING_MILD = ["rises", "falls", "beats", "misses", "warns", "cuts", "hikes",
               "ipo", "deal", "probe"]


def heuristic_rating(text):
    score = 2
    low = text.lower()
    score += 2 * sum(1 for w in RATING_STRONG if w in low)
    score += 1 * sum(1 for w in RATING_MILD if w in low)
    return max(1, min(5, score))


def condense_text(text, max_chars=220, max_sentences=2):
    """Condense extracted text to the first 1-2 sentences, truncated."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out = " ".join(sentences[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[:max_chars - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return out


def heuristic_summarize(candidates):
    """Fallback: top 3 by recency. Bullets come from the best available text
    (extracted article content, else cleaned RSS summary) — never the title."""
    out = []
    for item in candidates[:3]:
        if item.get("content"):
            bullets = [condense_text(item["content"])]
        elif item["summary"]:
            summary = item["summary"]
            bullets = [summary[:197] + "..." if len(summary) > 200 else summary]
        else:
            bullets = ["Summary not available."]
        out.append({
            "title": item["title"],
            "url": item["link"],
            "source": item["source"],
            "published": item["published"],
            "bullets": bullets,
            "rating": heuristic_rating(
                item["title"] + " " + (item.get("content") or item["summary"])),
            "reason": "Heuristic rating (no AI summary).",
        })
    return out


GEMINI_PROMPT = """You are a financial news analyst. Below are candidate news items for the category "{label}". Each candidate includes the article content (extracted full text where available, otherwise the RSS snippet).

Pick the 3 most important stories for a finance professional. For each, write 2-3 concise analyst-style bullet points and assign an importance rating from 1 (minor) to 5 (market-moving).

The bullet points MUST be digested from the provided article content: capture key facts, numbers, and implications for investors. They must add value beyond the headline — NEVER merely rephrase or restate the title.

Return STRICT JSON only: an array of exactly 3 objects, each with keys:
"title" (string), "url" (string, use the item's link), "source" (string),
"published" (string), "bullets" (array of 2-3 concise bullet strings),
"rating" (integer 1-5), "reason" (one short phrase justifying the rating).

Candidate items:
{items}
"""


def gemini_summarize(category, candidates):
    """One Gemini call per category. Raises on any failure."""
    lines = []
    for i, item in enumerate(candidates, 1):
        content = (item.get("content") or item["summary"])[:2500]
        lines.append(
            f"{i}. {item['title']}\n"
            f"   Source: {item['source']} | Date: {item['published']}\n"
            f"   Link: {item['link']}\n"
            f"   Content: {content}"
        )
    prompt = GEMINI_PROMPT.format(label=category["label"], items="\n".join(lines))
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }
    data = gemini_generate(payload)
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("Gemini response is not a JSON array")

    out = []
    for entry in parsed[:3]:
        bullets = entry.get("bullets") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        bullets = [str(b) for b in bullets][:3] or ["Summary not available."]
        try:
            rating = int(entry.get("rating", 3))
        except (TypeError, ValueError):
            rating = 3
        out.append({
            "title": str(entry.get("title", "")),
            "url": str(entry.get("url", "")),
            "source": str(entry.get("source", "")),
            "published": str(entry.get("published", "")),
            "bullets": bullets,
            "rating": max(1, min(5, rating)),
            "reason": str(entry.get("reason", "")),
        })
    if not out:
        raise ValueError("Gemini returned no usable items")
    return out


def summarize_category(category, candidates):
    if not candidates:
        log.warning("No candidates for %s", category["label"])
        return []
    if GEMINI_API_KEY:
        try:
            log.info("Summarizing with Gemini: %s", category["label"])
            return gemini_summarize(category, candidates)
        except Exception as exc:
            log.warning("Gemini failed for %s (%s); using heuristic fallback",
                        category["label"], exc)
    else:
        log.info("No GEMINI_API_KEY; heuristic summary: %s", category["label"])
    return heuristic_summarize(candidates)


# ---------------------------------------------------------------------------
# Market indicators
# ---------------------------------------------------------------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def short_date(year, month, day):
    """Short, locale-independent date label, e.g. 'Jul 15'."""
    return f"{_MONTHS[month - 1]} {day}"


def fetch_fred_2y():
    """US 2Y yield from FRED csv (no API key needed)."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2"
    resp = requests.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    lines = [l for l in resp.text.strip().splitlines() if l][1:]  # skip header
    points = []  # (label, value)
    for line in lines:
        parts = line.split(",")
        if len(parts) == 2 and parts[1] not in ("", "."):
            try:
                y, m, d = (int(x) for x in parts[0].split("-"))
                points.append((short_date(y, m, d), float(parts[1])))
            except ValueError:
                continue
    if not points:
        raise ValueError("no data in FRED csv")
    points = points[-SPARKLINE_POINTS:]
    labels = [p[0] for p in points]
    closes = [p[1] for p in points]
    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else None
    change_bp = round((last - prev) * 100, 1) if prev is not None else None
    return {"last": last, "change_bp": change_bp, "closes": closes,
            "labels": labels}


def fetch_indicator(item):
    """Fetch one indicator. Returns a dict; failures yield status n/a."""
    result = {
        "label": item["label"],
        "kind": item["kind"],
        "last": None,
        "change": None,      # % for prices
        "change_bp": None,   # bp for yields
        "closes": [],
        "labels": [],
    }
    try:
        if item.get("fred"):
            result.update(fetch_fred_2y())
            return result
        import yfinance as yf
        hist = yf.Ticker(item["ticker"]).history(period="1mo")
        if hist is None or hist.empty:
            raise ValueError("empty history")
        series = hist["Close"].dropna()
        series = series[-SPARKLINE_POINTS:]
        if series.empty:
            raise ValueError("no closes")
        closes = [round(float(c), 4) for c in series.tolist()]
        labels = [short_date(ts.year, ts.month, ts.day) for ts in series.index]
        result["closes"] = closes
        result["labels"] = labels
        result["last"] = closes[-1]
        if len(closes) > 1 and closes[-2]:
            prev = closes[-2]
            if item["kind"] == "yield":
                result["change_bp"] = round((closes[-1] - prev) * 100, 1)
            else:
                result["change"] = round((closes[-1] - prev) / prev * 100, 2)
        return result
    except Exception as exc:
        # yfinance can rate-limit (HTTP 429); failures must be non-fatal.
        log.warning("Indicator failed: %s (%s): %s",
                    item["label"], item["ticker"], exc)
        return result


def fetch_indicators():
    groups = []
    for group in INDICATOR_GROUPS:
        log.info("Fetching indicators: %s", group["name"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fetch_indicator, item): item
                       for item in group["items"]}
            results = {}
            for fut in as_completed(futures):
                item = futures[fut]
                results[item["label"]] = fut.result()
        groups.append({
            "name": group["name"],
            "items": [results[i["label"]] for i in group["items"]],
        })
    return groups


def build_snapshot(groups):
    """One-line market snapshot string for the page header."""
    wanted = {"S&P 500": "S&P", "Hang Seng": "HSI", "US 10Y": "US10Y",
              "US Dollar Index": "DXY"}
    parts = []
    for group in groups:
        for item in group["items"]:
            short = wanted.get(item["label"])
            if not short:
                continue
            if item["kind"] == "yield" and item["last"] is not None:
                parts.append(f"{short} {item['last']:.2f}%")
            elif item["change"] is not None:
                parts.append(f"{short} {item['change']:+.1f}%")
            elif item["last"] is not None:
                parts.append(f"{short} {item['last']:,.1f}")
    return " \u00b7 ".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_pages(template, page_date, generated_at, snapshot, takeaways,
                 news, groups):
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_PATH.parent)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    tpl = env.get_template(template)

    ctx = {
        "page_date": page_date,
        "generated_at": generated_at,
        "snapshot": snapshot,
        "takeaways": takeaways,
        "categories": news,
        "indicator_groups": groups,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    DAYS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    index_html = tpl.render(**ctx, base=".")
    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")
    log.info("Wrote %s", DOCS_DIR / "index.html")

    day_html = tpl.render(**ctx, base="..")
    day_path = DAYS_DIR / f"{page_date}.html"
    day_path.write_text(day_html, encoding="utf-8")
    log.info("Wrote %s", day_path)

    # Manifest: newest first; rerunning the same day must not duplicate.
    manifest_path = DOCS_DIR / "manifest.json"
    dates = []
    if manifest_path.exists():
        try:
            dates = json.loads(manifest_path.read_text(encoding="utf-8")).get("dates", [])
        except Exception as exc:
            log.warning("Could not read existing manifest (%s); rebuilding", exc)
            dates = []
    if page_date in dates:
        dates.remove(page_date)
    dates.insert(0, page_date)
    dates.sort(reverse=True)
    manifest_path.write_text(json.dumps({"dates": dates}, indent=2),
                             encoding="utf-8")
    log.info("Wrote %s (%d dates)", manifest_path, len(dates))

    # Raw structured data for debugging.
    raw = {
        "date": page_date,
        "generated_at": generated_at,
        "snapshot": snapshot,
        "takeaways": takeaways,
        "categories": news,
        "indicator_groups": groups,
    }
    data_path = DATA_DIR / f"{page_date}.json"
    data_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    log.info("Wrote %s", data_path)


# ---------------------------------------------------------------------------

def main():
    now_hk = datetime.now(HK_TZ)
    page_date = now_hk.strftime("%Y-%m-%d")
    generated_at = now_hk.strftime("%Y-%m-%d %H:%M HKT")
    log.info("Daily Market Brief for %s (Gemini: %s)", page_date,
             f"enabled, {gemini_provider()} provider" if GEMINI_API_KEY
             else "disabled, heuristic fallback")

    news = []
    for category in CATEGORIES:
        candidates = fetch_category_news(category)
        items = summarize_category(category, candidates)
        news.append({
            "id": category["id"],
            "label": category["label"],
            "items": items,
        })

    takeaways = build_takeaways(news)
    log.info("Takeaways: %d bullets", len(takeaways))

    groups = fetch_indicators()
    snapshot = build_snapshot(groups)
    log.info("Snapshot: %s", snapshot or "(empty)")

    render_pages("dashboard.html.j2", page_date, generated_at, snapshot,
                 takeaways, news, groups)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Unrecoverable error")
        sys.exit(1)
