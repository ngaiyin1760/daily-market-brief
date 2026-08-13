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

import argparse
import calendar
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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
# Cloudflare Worker proxy URL for browser-side Gemini calls (bypasses the HK
# geo-block without a VPN). Set WORKER_URL in the workflow env; empty = the
# Search page has no semantic mode.
WORKER_URL = os.environ.get("WORKER_URL", "").strip()
# Embedding model for semantic search (server-side precompute).
GEMINI_EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL",
                                    "gemini-embedding-001").strip()
# Default: gemini-3.1-flash-lite — the current low-tier flash-lite class.
# Its free tier (~500 req/day) comfortably covers the 12 calls/run, whereas
# gemini-2.5-flash's free tier is only 20 req/day on this account (exhausted
# by a single scheduled + manual run) and gemini-2.5-flash-lite was retired
# (404 "no longer available to new users" as of 2026-08-03).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
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
    requests; AI Studio accepts them). On HTTP 429 (rate limit) waits and
    retries up to 2 times — a transient per-minute spike shouldn't cost the
    AI summaries. Raises on any final failure, including the quota detail
    from Google's error body so the logs show WHICH cap was hit."""
    provider = gemini_provider()
    if provider == "vertex":
        url = ("https://aiplatform.googleapis.com/v1/publishers/google/models/"
               f"{GEMINI_MODEL}:generateContent")
        headers = {"x-goog-api-key": GEMINI_API_KEY}
    else:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
        headers = {}
    for attempt in range(3):
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        if resp.status_code == 429:
            if attempt < 2:
                wait = 65 * (attempt + 1)
                log.warning("Gemini rate-limited (429); retry %d/2 in %ds",
                            attempt + 1, wait)
                time.sleep(wait)
                continue
            raise requests.HTTPError(
                f"429 Too Many Requests: {resp.text[:500]}", response=resp)
        resp.raise_for_status()
        return resp.json()


def gemini_json(prompt, temperature=0.2):
    """One Gemini call that must return a JSON object; returns the parsed
    dict. Strips markdown fences (```json ... ```) the model sometimes wraps
    JSON in. Raises on any failure (callers decide whether to fall back)."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json",
                             "temperature": temperature},
    }
    data = gemini_generate(payload)
    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    # Strip ```json ... ``` fences and any surrounding prose.
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        text = m.group(0)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Gemini response is not a JSON object")
    return parsed

# ---------------------------------------------------------------------------
# News categories
# ---------------------------------------------------------------------------

CATEGORIES = [
    {"id": "global-macro", "label": "Global Economy",
     "query": "global economy OR central bank OR inflation", "top_n": 3},
    {"id": "equity", "label": "Equity Markets",
     "query": "stock market OR equities OR earnings", "top_n": 2},
    {"id": "fixed-income", "label": "Fixed Income",
     "query": "bond yields OR treasuries OR credit markets", "top_n": 2},
    {"id": "china", "label": "China Economy",
     "query": "China economy OR PBoC OR China GDP", "top_n": 2},
    {"id": "hk", "label": "Hong Kong Economy",
     "query": "Hong Kong economy OR HKEX OR Hong Kong property", "top_n": 2},
    {"id": "tech", "label": "Global Technology",
     "query": "big tech OR technology industry", "top_n": 2},
    {"id": "ai", "label": "Artificial Intelligence",
     "query": "artificial intelligence OR OpenAI OR LLM", "top_n": 2},
    {"id": "semis", "label": "Semiconductors",
     "query": "semiconductors OR TSMC OR NVIDIA OR chips", "top_n": 2},
    {"id": "ev", "label": "Electric Vehicles",
     "query": "electric vehicles OR Tesla OR BYD OR EV sales", "top_n": 2},
    {"id": "data-center", "label": "Data Centers",
     "query": "data centers OR hyperscale OR AI infrastructure", "top_n": 2},
    {"id": "space", "label": "Space & Aerospace",
     "query": "space industry OR SpaceX OR satellite OR rocket launch", "top_n": 2},
]

DEFAULT_TOP_N = 2


def normalize_title(title):
    """Normalize a headline for cross-category dedup: lowercase, strip the
    trailing " - <Source>" suffix Google News appends, remove punctuation and
    extra whitespace."""
    t = title.lower()
    t = re.sub(r"\s+-\s+[^-]{1,80}$", "", t)  # trailing " - Source"
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Hard freshness cutoff: the brief only ever shows stories published within
# the past 24 hours. If a category can't fill its slots from that window it
# runs short — stale news is never shown, by design.
MAX_NEWS_AGE_SECONDS = 24 * 3600


# Source tiers. Google News search returns everything from wire services to
# content farms, so publishers are grouped into tiers and a story's tier
# RANKS ABOVE ITS TIMELINESS: a Tier 1 story published 23h ago outranks a
# Tier 2 story published minutes ago. Within the same tier, recency decides.
# Matching is on the normalized publisher name (lowercase, alphanumerics
# only) so "Bloomberg.com" matches "bloomberg". The full ranking is
# documented in NEWS_SOURCES.md — keep the two in sync.
TIER_1_SOURCES = {
    # Global wires & top-tier financial press
    "reuters", "reuterscom",
    "bloomberg", "bloombergcom",
    "apnews", "associatedpress",
    "financialtimes", "ftcom",
    "thewallstreetjournal", "wsj", "wsjcom",
    "cnbc", "barrons", "marketwatch", "theeconomist",
    "nikkeiasia", "asianikkei", "nikkei",
}
TIER_2_SOURCES = {
    # Quality business press & general press with strong business desks
    "yahoofinance", "yahoofinanceuk", "investingcom", "fortune",
    "thebusinesstimes",
    "bbc", "bbcnews", "cnn", "abcnews", "abcnewscom", "cbsnews",
    "npr", "nprorg",
    "theguardian", "theguardiancom",
    "thenewyorktimes", "nytimes", "washingtonpost",
    "axios", "semafor", "globalnews", "globalnewsca", "financialpost",
    "theglobeandmail", "thetelegraph",
    "straitstimes", "thejapantimes", "taipeitimes",
    # Greater China / HK
    "southchinamorningpost", "scmp", "scmpcom",
    "caixinglobal", "caixin",
    "hongkongeconomictimes", "hket", "aastocks", "thestandard",
}
TIER_3_SOURCES = {
    # Sector trade press (tech / AI / semis / EV / space / data centers)
    "techcrunch", "theinformation", "venturebeat", "mittechnologyreview",
    "tomshardware", "trendforce", "digitimes",
    "insideevs", "cleantechnica", "electrek",
    "spacenews", "spacenewscom", "spaceflightnow", "nasaspaceflight",
    "nasaspaceflightcom", "spacecom", "payloadspace",
    "datacenterdynamics", "datacenterknowledge",
    # Specialized analysts & state-owned national press
    "chinadaily", "chinadailyglobaledition",
    "seekingalpha", "foxbusiness", "councilonforeignrelations",
}

# tier index = rank; anything not listed is unranked (ranks last).
SOURCE_TIERS = (TIER_1_SOURCES, TIER_2_SOURCES, TIER_3_SOURCES)


def normalize_source(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def source_tier(item):
    """Tier of an item's publisher: 0 = Tier 1, 1 = Tier 2, 2 = Tier 3,
    99 = unranked (any outlet not in the tier lists). Lower is better.
    A ranking signal only — never a hard filter, so a category can still
    fill from unranked outlets when no tiered source covered the topic
    within the 24h window."""
    name = normalize_source(item.get("source"))
    for tier, sources in enumerate(SOURCE_TIERS):
        if name in sources:
            return tier
    return 99


def is_fresh(item, now):
    """True only when the item carries a publish timestamp within the past
    24 hours. Undated items and anything older are excluded outright — the
    brief runs a category short rather than showing stale news. Epoch
    comparisons are timezone-safe (both sides are absolute UTC)."""
    ts = item.get("_ts") or 0
    return bool(ts) and now - ts <= MAX_NEWS_AGE_SECONDS


def prepare_candidates(candidates, claimed, now):
    """Order a category's candidates for selection.

    HARD freshness cutoff: items older than 24 hours, or with no parseable
    publish time, are dropped outright — a category runs short rather than
    showing stale news. Within the 24h window:
    - Within-category dedup: drop exact normalized-title repeats (Google News
      clusters the same story across outlets), keeping the copy from the
      best-tier outlet (newest copy breaks ties). Items with empty/missing
      titles BYPASS dedup and are never dropped.
    - Cross-category/cross-day dedup: titles already claimed by an earlier
      category or a previous day's brief are DEMOTED, not dropped —
      preference order is non-duplicates before duplicates, then source tier
      (Tier 1 -> 2 -> 3 -> unranked), then recency. A dupe is only used when
      nothing unique can fill the category's slots.
    Returns (ordered_candidates, stats_dict).
    """
    seen_local = {}
    unique = []
    stale_dropped = 0
    within_dropped = 0
    for item in candidates:
        if not is_fresh(item, now):
            stale_dropped += 1
            continue
        key = normalize_title(item["title"])
        if key:
            if key in seen_local:
                # Same story from another outlet — keep the copy from the
                # best-tier source (newer copy breaks a tie), not just the
                # first one the feed happened to list.
                idx = seen_local[key]
                kept = unique[idx]
                if (source_tier(item), -item["_ts"]) < \
                        (source_tier(kept), -kept["_ts"]):
                    item["_key"] = key
                    unique[idx] = item
                within_dropped += 1
                continue
            seen_local[key] = len(unique)
        item["_key"] = key
        unique.append(item)

    cross_dupes = 0
    ranked = []
    for item in unique:
        is_dupe = bool(item["_key"]) and item["_key"] in claimed
        if is_dupe:
            cross_dupes += 1
        ranked.append((1 if is_dupe else 0, source_tier(item),
                       -item["_ts"], item))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))

    stats = {
        "candidates": len(unique),
        "stale_dropped": stale_dropped,
        "tier1": sum(1 for i in unique if source_tier(i) == 0),
        "tier2": sum(1 for i in unique if source_tier(i) == 1),
        "tier3": sum(1 for i in unique if source_tier(i) == 2),
        "cross_dupes": cross_dupes,
        "within_dropped": within_dropped,
    }
    return [r[3] for r in ranked], stats


CROSS_DAY_DEDUP_DAYS = 3


def load_recent_titles(page_date_str, days=CROSS_DAY_DEDUP_DAYS):
    """Normalized titles from the previous `days` daily briefs. Used to seed
    cross-category dedup so a story already covered on an earlier day ranks
    below anything fresh — it can still fill a slot as a last resort, so a
    category never goes empty, but routine repeats across days are avoided.
    Today's own file is deliberately NOT loaded: re-running the same day
    regenerates that day's brief and must not exclude its own picks."""
    titles = set()
    try:
        base = datetime.strptime(page_date_str, "%Y-%m-%d")
    except ValueError:
        return titles
    for offset in range(1, days + 1):
        day = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = DATA_DIR / f"{day}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Cross-day dedup: could not read %s: %s", path, exc)
            continue
        for cat in data.get("categories", []):
            for item in cat.get("items", []):
                key = normalize_title(item.get("title", ""))
                if key:
                    titles.add(key)
    log.info("Cross-day dedup: seeded %d title(s) from previous %d day(s)",
             len(titles), days)
    return titles

# ---------------------------------------------------------------------------
# Daily key takeaways
# ---------------------------------------------------------------------------

TAKEAWAYS_PROMPT = """You are a financial news analyst writing the lead summary of a daily market brief.

Below are the day's selected top stories across all coverage categories, with their importance ratings and the key points digested from each article.

Write exactly 3 concise analyst-style bullet points synthesizing the day's most important cross-market findings — connect themes across categories where relevant. Synthesize insights from the key points; do NOT just list or restate headlines. Keep each bullet tight (max ~30 words).

Return STRICT JSON only: an object with one key "takeaways", an array of exactly 3 bullet strings.

Top stories:
{items}
"""


def heuristic_takeaways(news):
    """Fallback: 3 highest-rated items across all categories (ties broken
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
    return [f"[{label}] {text}" for _, label, text in ranked[:3]]


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
    return [str(t) for t in takeaways][:3]


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
# Day importance rating (1-5) — "how hard should I read today?"
# ---------------------------------------------------------------------------

DAY_IMPORTANCE_PROMPT = """You are a market editor deciding how significant today's news day is for a finance/tech professional.

Below are today's top stories (with ratings) and the day's biggest market moves. Assess how important TODAY is overall, on a 1-5 scale where:
1 = routine, nothing major — skim
2 = light
3 = moderate
4 = significant — several meaningful developments
5 = major — market-moving events (big policy, crisis, major earnings/CPI)

Also give a one-sentence "verdict" that tells the reader at a glance whether today is a skim day or a read-carefully day, and WHY (mention the 1-2 biggest drivers).

Return STRICT JSON only: {"importance": <1-5 int>, "verdict": "<one sentence>"}.

Today's stories:
{stories}

Market moves (daily):
{moves}
"""


def day_importance_input(news, groups):
    """Compact text inputs for the day-importance call."""
    story_lines = []
    for cat in news:
        for item in cat["items"]:
            story_lines.append(
                f"- [{cat['label']}] ({item['rating']}/5) {item['title']}")
    move_lines = []
    for grp in groups:
        for it in grp["items"]:
            if it["last"] is None:
                continue
            if it["kind"] == "yield" and it.get("change_bp") is not None:
                move_lines.append(
                    f"- {it['label']}: {it['last']}% ({it['change_bp']:+.1f} bp)")
            elif it.get("change") is not None:
                move_lines.append(
                    f"- {it['label']}: {it['last']:,.2f} ({it['change']:+.2f}%)")
    return "\n".join(story_lines) or "(no stories)", "\n".join(move_lines) or "(no moves)"


def gemini_day_importance(news, groups):
    """One Gemini call -> (importance int 1-5, verdict string). Raises on fail."""
    stories, moves = day_importance_input(news, groups)
    prompt = DAY_IMPORTANCE_PROMPT.format(stories=stories, moves=moves)
    parsed = gemini_json(prompt)
    imp = int(parsed.get("importance", 3))
    imp = max(1, min(5, imp))
    verdict = str(parsed.get("verdict", "")).strip()
    if not verdict:
        raise ValueError("Gemini day importance missing verdict")
    return imp, verdict


def heuristic_day_importance(news, groups):
    """Fallback: mean of top story ratings + notable moves -> 1-5."""
    ratings = [it["rating"] for cat in news for it in cat["items"]]
    score = 2.0
    if ratings:
        score = sum(ratings) / len(ratings)
    strong = sum(1 for r in ratings if r >= 4)
    if strong >= 2:
        score += 1.0
    notable = 0
    for grp in groups:
        for it in grp["items"]:
            chg = it.get("change_bp") if it["kind"] == "yield" else it.get("change")
            if chg is not None and abs(chg) >= 1.5:
                notable += 1
    if notable >= 3:
        score += 0.5
    imp = max(1, min(5, round(score)))
    labels = {1: "Quiet day — nothing major moved; skim today.",
              2: "Light day — a few items worth a look.",
              3: "Moderate day — some meaningful developments.",
              4: "Significant day — several important developments.",
              5: "Major day — market-moving events; read carefully."}
    return imp, labels[imp]


def build_day_importance(news, groups):
    if GEMINI_API_KEY:
        try:
            log.info("Generating day importance with Gemini")
            return gemini_day_importance(news, groups)
        except Exception as exc:
            log.warning("Gemini day importance failed (%s); heuristic", exc)
    else:
        log.info("No GEMINI_API_KEY; heuristic day importance")
    return heuristic_day_importance(news, groups)


# ---------------------------------------------------------------------------
# Weekly "5 things that mattered" (Fridays)
# ---------------------------------------------------------------------------

WEEKLY_ISOWEEKDAY = 5   # Friday

WEEKLY_PROMPT = """You are a market editor writing the Friday wrap-up. Below are the week's top stories, numbered 1..N across all days ({n_days} days: {dates}).

Pick the 5 most important STORIES this week for a finance/tech professional — the things that will still matter next week. Prioritize the biggest stories by their rating and impact; if several of the biggest all happened on one day, that's fine — importance wins over day diversity. Do NOT write your own text; simply return the numbers (indices) of the 5 stories you pick, in order of importance.

Return STRICT JSON only: {"week": "<YYYY-MM-DD to YYYY-MM-DD>", "items": [5 integers, the indices of the chosen stories]}.

This week's data:
{data}
"""


def gemini_weekly(news_days, dates_str, week_label):
    """One Gemini call for the weekly 5. Raises on failure.

    Gemini selects by index (1-based across all week items); each returned
    entry is mapped back to its source item so it carries title, url, and
    the digest bullet."""
    lines = []
    idx = 1
    index_map = {}   # idx -> {date, label, item}
    for day in news_days:
        lines.append(f"== {day['date']} ==")
        for cat in day["categories"]:
            for it in cat["items"]:
                lines.append(f"{idx}. [{cat['label']}] ({it.get('rating',0)}/5) {it.get('title','')}")
                index_map[idx] = {"date": day["date"], "label": cat.get("label", ""),
                                  "item": it}
                idx += 1
    prompt = WEEKLY_PROMPT.format(n_days=len(news_days), dates=dates_str,
                                  data="\n".join(lines))
    parsed = gemini_json(prompt)
    items = parsed.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("Gemini weekly items missing or not a list")

    out = []
    for entry in items[:5]:
        # entry may be an index (int) or a short text containing the index.
        sel = None
        if isinstance(entry, (int, float)):
            sel = int(entry)
        elif isinstance(entry, str):
            m = re.match(r"^\s*(\d+)", entry)
            if m:
                sel = int(m.group(1))
        src = index_map.get(sel) if sel else None
        if not src:
            continue
        bullets = src["item"].get("bullets") or []
        out.append({
            "day": datetime.strptime(src["date"], "%Y-%m-%d").strftime("%a"),
            "label": src["label"],
            "title": src["item"].get("title", ""),
            "url": src["item"].get("url", ""),
            "bullet": bullets[0] if bullets else "",
        })
    if len(out) < 2:
        raise ValueError("Gemini weekly didn't map to enough source items")
    return {"week": str(parsed.get("week", week_label)), "items": out}


def heuristic_weekly(news_days, week_label):
    """Fallback: the 5 highest-rated items across the whole week, period.

    Importance is the only signal — if all 5 most important events happened
    on Monday, all 5 are Monday. Day diversity is deliberately NOT enforced:
    the point is the week's most important 5, not one-per-day. Ties break by
    the day order given (news_days is oldest-first, so earlier days win a
    rating tie).

    Each selected item carries its title, url (hyperlink), and first bullet
    from the daily digest, so the weekly shows substance + a link, not just
    a headline."""
    all_items = []
    for day in news_days:
        for cat in day["categories"]:
            for it in cat["items"]:
                all_items.append({
                    "rating": it.get("rating", 0),
                    "date": day["date"],
                    "label": cat.get("label", ""),
                    "title": it.get("title", ""),
                    "url": it.get("url", ""),
                    "bullets": it.get("bullets") or [],
                })
    all_items.sort(key=lambda x: x["rating"], reverse=True)
    out = []
    for it in all_items[:5]:
        dow = datetime.strptime(it["date"], "%Y-%m-%d").strftime("%a")
        out.append({
            "day": dow,
            "label": it["label"],
            "title": it["title"],
            "url": it["url"],
            "bullet": it["bullets"][0] if it["bullets"] else "",
        })
    return {"week": week_label, "items": out}


def load_week_days(end_date_str, days=5):
    """Load the previous `days` daily briefs (including end_date), newest
    first; skips missing dates. Returns [{date, categories}]."""
    out = []
    try:
        end = datetime.strptime(end_date_str, "%Y-%m-%d")
    except ValueError:
        return out
    for offset in range(days - 1, -1, -1):
        day = (end - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = DATA_DIR / f"{day}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({"date": day, "categories": data.get("categories", [])})
        except Exception as exc:
            log.warning("Weekly: could not read %s: %s", path, exc)
    return out


def build_weekly(page_date, news_days):
    """Build the weekly-5 block. Returns None when not a Friday or when
    fewer than 3 days of briefs are available (not enough signal)."""
    try:
        dt = datetime.strptime(page_date, "%Y-%m-%d")
    except ValueError:
        return None
    if dt.isoweekday() != WEEKLY_ISOWEEKDAY or not news_days:
        return None
    if len(news_days) < 3:
        log.info("Weekly: only %d day(s) of briefs; skipping", len(news_days))
        return None
    monday = (dt - timedelta(days=4)).strftime("%Y-%m-%d")
    week_label = f"{monday} to {page_date}"
    dates_str = ", ".join(d["date"] for d in news_days)
    if GEMINI_API_KEY:
        try:
            log.info("Generating weekly 5 with Gemini")
            return gemini_weekly(news_days, dates_str, week_label)
        except Exception as exc:
            log.warning("Gemini weekly failed (%s); heuristic", exc)
    else:
        log.info("No GEMINI_API_KEY; heuristic weekly 5")
    return heuristic_weekly(news_days, week_label)
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
            {"label": "USD/CNY", "ticker": "CNY=X", "kind": "price"},
            {"label": "USD/HKD", "ticker": "HKD=X", "kind": "price"},
            {"label": "HKD/CNY", "ticker": "HKDCNY=X", "kind": "price"},
            # JPY/HKD quoted per 100 JPY (1 JPY ~ 0.05 HKD would be unreadable);
            # mult scales the raw cross rate into "100 JPY = X HKD".
            {"label": "JPY/HKD (¥100)", "ticker": "JPYHKD=X", "kind": "price", "mult": 100},
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

CHART_MAX_POINTS = 2600  # ~10 years of trading days — Markets tab charts use up to a 10y window

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
    """Fetch up to 25 RSS items for a category, sorted by recency.
    The 24h cutoff and source-quality ranking happen later in
    prepare_candidates; a wider pool gives trusted outlets more chances
    to be represented."""
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

    # Sort by recency; the 24h cutoff and source-quality ranking happen at
    # selection time (see prepare_candidates).
    items.sort(key=lambda i: i["_ts"], reverse=True)
    return items[:25]


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


def heuristic_summarize(candidates, top_n=DEFAULT_TOP_N):
    """Fallback: top N by recency. Bullets come from the best available text
    (extracted article content, else cleaned RSS summary) — never the title."""
    out = []
    for item in candidates[:top_n]:
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

Pick the {n} most important stories for a finance professional. For each, write exactly 2 bullet points and assign an importance rating from 1 (minor) to 5 (market-moving).

The bullet points MUST be digested from the provided article content: capture key facts, numbers, and implications for investors. They must add value beyond the headline — NEVER merely rephrase or restate the title. Each bullet should be one to two full sentences (roughly 25-40 words): substantive enough to stand on its own, but no padding or repetition.

Return STRICT JSON only: an array of exactly {n} objects, each with keys:
"title" (string), "url" (string, use the item's link), "source" (string),
"published" (string), "bullets" (array of exactly 2 bullet strings),
"rating" (integer 1-5), "reason" (one short phrase justifying the rating).

Candidate items:
{items}
"""


def gemini_summarize(category, candidates, top_n=DEFAULT_TOP_N):
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
    prompt = GEMINI_PROMPT.format(label=category["label"], n=top_n,
                                  items="\n".join(lines))
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
    for entry in parsed[:top_n]:
        bullets = entry.get("bullets") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        bullets = [str(b) for b in bullets][:2] or ["Summary not available."]
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


AI_SUMMARIES = {"ok": 0, "heuristic": 0}


def summarize_category(category, candidates):
    top_n = category.get("top_n", DEFAULT_TOP_N)
    if not candidates:
        log.warning("No candidates for %s", category["label"])
        return []
    if GEMINI_API_KEY:
        try:
            log.info("Summarizing with Gemini: %s", category["label"])
            AI_SUMMARIES["ok"] += 1
            return gemini_summarize(category, candidates, top_n)
        except Exception as exc:
            log.warning("Gemini failed for %s (%s); using heuristic fallback",
                        category["label"], exc)
    else:
        log.info("No GEMINI_API_KEY; heuristic summary: %s", category["label"])
    AI_SUMMARIES["heuristic"] += 1
    return heuristic_summarize(candidates, top_n)


# ---------------------------------------------------------------------------
# Market indicators
# ---------------------------------------------------------------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def short_date(year, month, day, with_year=False):
    """Short, locale-independent date label, e.g. 'Jul 15' or "Jul 15 '25"."""
    s = f"{_MONTHS[month - 1]} {day}"
    return f"{s} '{year % 100:02d}" if with_year else s


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
                points.append((short_date(y, m, d, with_year=True), float(parts[1])))
            except ValueError:
                continue
    if not points:
        raise ValueError("no data in FRED csv")
    points = points[-CHART_MAX_POINTS:]
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
        hist = yf.Ticker(item["ticker"]).history(period="10y")
        if hist is None or hist.empty:
            raise ValueError("empty history")
        series = hist["Close"].dropna()
        series = series[-CHART_MAX_POINTS:]
        if series.empty:
            raise ValueError("no closes")
        closes = [round(float(c) * item.get("mult", 1), 4)
                  for c in series.tolist()]
        labels = [short_date(ts.year, ts.month, ts.day, with_year=True)
                  for ts in series.index]
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
    """Snapshot ticker items for the page header: list of {"text", "cls"}
    where cls is pos/neg/flat based on the daily change sign."""
    wanted = {"S&P 500": "S&P", "Nasdaq": "Nasdaq", "Hang Seng": "HSI",
              "US 10Y": "US10Y", "USD/HKD": "USD/HKD", "HKD/CNY": "HKD/CNY",
              "JPY/HKD (¥100)": "JPY/HKD(¥100)", "Gold": "Gold", "Bitcoin": "BTC"}
    parts = []
    for group in groups:
        for item in group["items"]:
            short = wanted.get(item["label"])
            if not short:
                continue
            if item["kind"] == "yield" and item["last"] is not None:
                text = f"{short} {item['last']:.2f}%"
                chg = item["change_bp"]
            elif item["change"] is not None:
                text = f"{short} {item['change']:+.1f}%"
                chg = item["change"]
            elif item["last"] is not None:
                text = f"{short} {item['last']:,.1f}"
                chg = None
            else:
                continue
            if chg is None or round(chg, 1) == 0:
                cls = "flat"
            else:
                cls = "pos" if chg > 0 else "neg"
            # Arrow follows the DAILY change (same direction/color as the %
            # text) so the ticker reads consistently — no 1-month trend that
            # can contradict today's move. (1-month info lives on the hero
            # sparklines and the Markets card chips instead.)
            m1 = {"pos": "up", "neg": "down", "flat": "flat"}[cls]
            parts.append({"text": text, "cls": cls, "m1": m1})
    return parts


def write_indicators_json(groups, generated_at):
    """Full indicator history (up to 10y) for the Markets tab. Written fresh
    on every run so the tab always shows the latest data; the file is
    replaced, not appended, so repo size stays bounded once 10y fills in."""
    payload = {
        "generated_at": generated_at,
        "groups": [
            {
                "name": group["name"],
                "items": [
                    {
                        "label": item["label"],
                        "kind": item["kind"],
                        "last": item["last"],
                        "change": item["change"],
                        "change_bp": item["change_bp"],
                        "closes": item["closes"],
                        "labels": item["labels"],
                    }
                    for item in group["items"]
                ],
            }
            for group in groups
        ],
    }
    path = DATA_DIR / "indicators.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                    encoding="utf-8")
    log.info("Wrote %s", path)


def indicator_summary(groups):
    """Per-day JSON keeps only the headline numbers (no 10y closes/labels —
    those live in data/indicators.json for the Markets tab)."""
    return [
        {
            "name": group["name"],
            "items": [
                {
                    "label": item["label"],
                    "kind": item["kind"],
                    "last": item["last"],
                    "change": item["change"],
                    "change_bp": item["change_bp"],
                }
                for item in group["items"]
            ],
        }
        for group in groups
    ]


# ---------------------------------------------------------------------------
# "What changed" helpers: NEW story tags and the hero snapshot card.
# ---------------------------------------------------------------------------

def load_day_titles(page_date_str, offset):
    """Normalized titles from exactly ONE previous daily brief (offset days
    back). Used to tag today's stories as NEW (absent from yesterday's brief)."""
    titles = set()
    try:
        base = datetime.strptime(page_date_str, "%Y-%m-%d")
    except ValueError:
        return titles
    day = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
    path = DATA_DIR / f"{day}.json"
    if not path.exists():
        return titles
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return titles
    for cat in data.get("categories", []):
        for item in cat.get("items", []):
            key = normalize_title(item.get("title", ""))
            if key:
                titles.add(key)
    return titles


def load_day_indicators(page_date_str, offset=1):
    """label -> {"last", "kind"} from a previous day's brief (for the
    "Δ vs yesterday" deltas on the hero card)."""
    try:
        base = datetime.strptime(page_date_str, "%Y-%m-%d")
    except ValueError:
        return {}
    day = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
    path = DATA_DIR / f"{day}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for gr in data.get("indicator_groups", []):
        for it in gr.get("items", []):
            out[it.get("label")] = {
                "last": it.get("last"),
                "kind": it.get("kind"),
            }
    return out


# Hero card instruments: (indicator label, short display name).
HERO_ITEMS = [
    ("S&P 500", "S&P 500"), ("Nasdaq", "Nasdaq"), ("Hang Seng", "HSI"),
    ("US 10Y", "US10Y"), ("USD/HKD", "USD/HKD"), ("HKD/CNY", "HKD/CNY"),
    ("JPY/HKD (¥100)", "JPY/HKD"), ("Gold", "Gold"), ("Bitcoin", "BTC"),
]


def build_sparkline(closes, width=100, height=32):
    """SVG polyline path for a ~1-month sparkline. Returns None if too short."""
    if len(closes) < 2:
        return None
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0
    n = len(closes)
    pts = []
    for i, c in enumerate(closes):
        x = round(i / (n - 1) * (width - 2) + 1, 1)
        y = round(height - 2 - (c - lo) / span * (height - 4), 1)
        pts.append(f"{x},{y}")
    return "M" + " L".join(pts)


def build_hero(groups, prev_indicators):
    """Structured at-a-glance data for the daily-page snapshot card."""
    by_label = {}
    for gr in groups:
        for it in gr["items"]:
            by_label[it["label"]] = it
    hero = []
    for label, short in HERO_ITEMS:
        item = by_label.get(label)
        if not item or item.get("last") is None:
            continue
        kind = item["kind"]
        last = item["last"]
        chg = item.get("change_bp") if kind == "yield" else item.get("change")
        # Delta vs yesterday's brief (bp for yields, % for prices).
        delta = None
        prev = prev_indicators.get(label)
        if prev and prev.get("last") is not None and prev["last"]:
            if kind == "yield":
                delta = round((last - prev["last"]) * 100, 1)
            else:
                delta = round((last - prev["last"]) / prev["last"] * 100, 2)
        if kind == "yield":
            last_display = f"{last:,.2f}%"
            chg_display = (f"{chg:+.1f} bp" if chg is not None else "—")
            delta_display = (f"{delta:+.1f} bp" if delta is not None else None)
        else:
            last_display = f"{last:,.2f}"
            chg_display = (f"{chg:+.2f}%" if chg is not None else "—")
            delta_display = (f"{delta:+.2f}%" if delta is not None else None)
        closes = (item.get("closes") or [])[-30:]
        m1 = None
        if len(closes) >= 22 and closes[-22]:
            m1_chg = (closes[-1] - closes[-22]) / closes[-22]
            m1 = ("up" if m1_chg > 0.001
                  else "down" if m1_chg < -0.001 else "flat")
        if chg is None or round(chg, 1) == 0:
            cls = "flat"
        else:
            cls = "pos" if chg > 0 else "neg"
        hero.append({
            "short": short,
            "label": label,
            "kind": kind,
            "last_display": last_display,
            "chg_display": chg_display,
            "chg": chg,
            "cls": cls,
            "delta": delta,
            "delta_display": delta_display,
            "spark": build_sparkline(closes),
            "m1": m1,
        })
    return hero


# ---------------------------------------------------------------------------
# Analytics (blog digest) & Repo Radar (GitHub trending) tabs
# ---------------------------------------------------------------------------

ANALYTICS_SOURCES_PATH = ROOT / "analytics_sources.md"
ANALYTICS_STATE_PATH = DATA_DIR / "analytics_state.json"
ANALYTICS_DATA_PATH = DATA_DIR / "analytics.json"
ANALYTICS_HISTORY_PATH = DATA_DIR / "analytics_history.json"
REPO_RADAR_CONFIG_PATH = ROOT / "repo_radar.md"
REPO_RADAR_DATA_PATH = DATA_DIR / "repo_radar.json"
REPO_RADAR_HISTORY_PATH = DATA_DIR / "repo_radar_history.json"
SEARCH_INDEX_PATH = DATA_DIR / "search_index.json"

ANALYTICS_DAILY_MAX = 5               # max posts shown per day (top-N by importance)
ANALYTICS_BACKLOG_MAX_AGE_DAYS = 7    # carried-forward posts older than this are dropped
ANALYTICS_FETCH_WORKERS = 8
ANALYTICS_MAX_PER_FEED = 30    # recent entries considered per blog per run
ANALYTICS_EXTRACT_TOP_N = 8    # fresh posts per blog that get full-text extraction
ANALYTICS_MAX_NEW_TOTAL = 60   # global cap on fresh posts summarized in one run
ANALYTICS_MAX_PER_BLOG_CALL = 10  # token safeguard: cap posts sent per AI call

REPO_RADAR_DAYS = 30           # repos created within the last N days
REPO_RADAR_MIN_STARS = 50
REPO_RADAR_PER_TOPIC = 10
REPO_RADAR_PICK = 3
REPO_RADAR_DEFAULT_TOPICS = [
    ("fintech", "fintech OR quant OR trading OR defi"),
    ("ai-llm", "llm OR ai-agent"),
    ("infra", "infrastructure OR devtools"),
]

GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()


# --- Analytics: sources + feed discovery ------------------------------------

def load_analytics_sources():
    """Parse analytics_sources.md -> [(name, url)]. Lines must be
    '- Name — https://url' (em/en dash or spaced hyphen). Blank lines,
    comments (#), and table rows (|) are ignored. Missing file -> []."""
    sources = []
    try:
        lines = ANALYTICS_SOURCES_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("Analytics sources missing: %s", exc)
        return sources
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("#", "|")):
            continue
        m = re.match(r"^-\s+(.+?)\s+[—-]\s+(\S+)\s*$", s)
        if m and m.group(2).startswith("http"):
            sources.append((m.group(1).strip(), m.group(2).strip()))
    return sources


_ALT_LINK_RE = re.compile(r"<link[^>]+rel=[\"']?alternate[\"']?[^>]*>", re.I)
_ALT_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_ALT_TYPE_RE = re.compile(r'type=["\']application/(rss|atom)\+xml["\']', re.I)


def _alternate_feed_urls(html, page_url):
    """Feed URLs advertised by <link rel=alternate type=...rss/atom> tags."""
    out = []
    for tag in _ALT_LINK_RE.findall(html or ""):
        if not _ALT_TYPE_RE.search(tag):
            continue
        m = _ALT_HREF_RE.search(tag)
        if m:
            out.append(urllib.parse.urljoin(page_url, m.group(1)))
    return out


def _check_feed_url(feed_url):
    """Return feed_url when it parses as a feed with entries, else None."""
    try:
        resp = requests.get(feed_url, timeout=HTTP_TIMEOUT,
                            headers={"User-Agent": BROWSER_UA})
        if not resp.ok:
            return None
        parsed = feedparser.parse(resp.content)
        return feed_url if getattr(parsed, "entries", None) else None
    except Exception:
        return None


def discover_feed(url):
    """Find a usable feed URL for a source page, or None. Order:
    1) the URL itself if it parses as a feed, 2) <link rel=alternate> tags
    advertised by the page, 3) conventional /feed, /rss, /atom.xml paths."""
    direct = _check_feed_url(url)
    if direct:
        return direct
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT,
                            headers={"User-Agent": BROWSER_UA})
        if resp.ok:
            for alt in _alternate_feed_urls(resp.text, url):
                found = _check_feed_url(alt)
                if found:
                    return found
    except Exception:
        pass
    base = url.rstrip("/")
    for candidate in (base + "/feed", base + "/rss", base + "/atom.xml",
                      base + "/feed/"):
        found = _check_feed_url(candidate)
        if found:
            return found
    return None


def fetch_feed_entries(feed_url, max_items=ANALYTICS_MAX_PER_FEED):
    """Fetch one feed; up to max_items entries: title/link/summary/published/_ts."""
    try:
        resp = requests.get(feed_url, timeout=HTTP_TIMEOUT,
                            headers={"User-Agent": BROWSER_UA})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:
        log.warning("Feed fetch failed for %s: %s", feed_url, exc)
        return []
    items = []
    for entry in parsed.entries[:max_items]:
        ts = None
        p = getattr(entry, "published_parsed", None) or getattr(
            entry, "updated_parsed", None)
        if p:
            ts = calendar.timegm(p)
        items.append({
            "title": strip_html(getattr(entry, "title", "")),
            "link": getattr(entry, "link", "") or "",
            "summary": strip_html(getattr(entry, "summary", "") or ""),
            "published": (datetime.utcfromtimestamp(ts)
                          .strftime("%Y-%m-%d %H:%M UTC")) if ts else "",
            "_ts": ts or 0,
        })
    return items


def analytics_post_key(blog_name, post):
    """Stable per-post identity for never-repeat dedup: blog + link (or title)."""
    link = post.get("link") or ""
    if link:
        return f"{blog_name}|{link}"
    title = normalize_title(post.get("title", ""))
    return f"{blog_name}|{title}" if title else f"{blog_name}|{post.get('title','')}"


def _sanitize_post(post):
    """Drop bulky internal fields (article text) before persisting/rendering."""
    keys = ["_key", "blog", "title", "link", "summary", "published", "_ts",
            "preview", "bullets", "rating"]
    return {k: post.get(k) for k in keys if k in post}


# --- Analytics: carry-forward backlog ----------------------------------------

def load_analytics_backlog():
    """Backlog of posts not yet shown, from docs/data/analytics_state.json.
    Missing/corrupt -> empty list."""
    try:
        data = json.loads(ANALYTICS_STATE_PATH.read_text(encoding="utf-8"))
        return [p for p in data.get("backlog", []) if isinstance(p, dict)]
    except Exception:
        return []


def save_analytics_backlog(backlog):
    """Persist the backlog. Non-fatal: a write failure loses the backlog for
    one run, not more."""
    try:
        ANALYTICS_STATE_PATH.write_text(
            json.dumps({"backlog": backlog}, indent=1, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write analytics backlog: %s", exc)


# --- Analytics: cumulative history ------------------------------------------

def update_analytics_history(page_date, generated_at, blogs_meta):
    """Append today's selected posts to the cumulative history file.

    The history is a durable, queryable record of every day's shown posts
    (title, link, bullets, rating, source, preview flag) — for future use
    (RAG, analysis). Newest first; re-running the same day overwrites that
    day's entry (idempotent). Zero-post days are recorded too, so a missing
    date means the pipeline didn't run, not that nothing was found."""
    days = []
    try:
        data = json.loads(ANALYTICS_HISTORY_PATH.read_text(encoding="utf-8"))
        days = data.get("days", []) if isinstance(data, dict) else []
    except Exception:
        days = []
    posts = []
    for blog in blogs_meta:
        posts.extend(blog.get("posts", []))
    days = [d for d in days if d.get("date") != page_date]
    days.insert(0, {"date": page_date, "generated_at": generated_at,
                    "posts": posts})
    days.sort(key=lambda d: d.get("date", ""), reverse=True)
    try:
        ANALYTICS_HISTORY_PATH.write_text(
            json.dumps({"days": days}, indent=1, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write analytics history: %s", exc)


# --- Repo Radar: cumulative history -----------------------------------------

def update_repo_radar_history(page_date, generated_at, repos):
    """Append today's selected repos to the cumulative history file, newest
    first; re-running the same day overwrites that day's entry. Zero-repo
    days are recorded too (a missing date means the pipeline didn't run)."""
    days = []
    try:
        data = json.loads(REPO_RADAR_HISTORY_PATH.read_text(encoding="utf-8"))
        days = data.get("days", []) if isinstance(data, dict) else []
    except Exception:
        days = []
    days = [d for d in days if d.get("date") != page_date]
    days.insert(0, {"date": page_date, "generated_at": generated_at,
                    "repos": repos})
    days.sort(key=lambda d: d.get("date", ""), reverse=True)
    try:
        REPO_RADAR_HISTORY_PATH.write_text(
            json.dumps({"days": days}, indent=1, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write repo radar history: %s", exc)


def _group_posts_by_blog(posts):
    """Group a flat post list by blog, blogs ordered by best-rated post,
    posts within a blog ordered by rating desc."""
    by_blog = {}
    for post in posts:
        by_blog.setdefault(post.get("blog") or "Other", []).append(post)
    return [
        {"name": name,
         "posts": sorted(by_blog[name], key=lambda p: -(p.get("rating") or 0))}
        for name in sorted(by_blog,
                           key=lambda n: -max(p.get("rating", 0)
                                              for p in by_blog[n]))
    ]


# ---------------------------------------------------------------------------
# Search index (keyword + semantic embeddings) for the Search page
# ---------------------------------------------------------------------------

SEARCH_INDEX_VERSION = 2   # bump = re-embed all items (embed input changed)


def embed_texts(texts):
    """Batch-embed a list of strings with Gemini's embedding model, using the
    server-side key. Returns a list of vectors (same order). Raises on
    failure (caller decides whether to degrade to keyword-only)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("No GEMINI_API_KEY for embeddings")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_EMBED_MODEL}:batchEmbedContents?key={GEMINI_API_KEY}")
    payload = {"requests": [{"model": f"models/{GEMINI_EMBED_MODEL}",
                             "content": {"parts": [{"text": t}]}}
                            for t in texts]}
    resp = requests.post(url, json=payload, headers={}, timeout=60)
    if resp.status_code == 429:
        time.sleep(5)
        resp = requests.post(url, json=payload, headers={}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    embs = data.get("embeddings") or []
    return [e.get("values", []) for e in embs]


def search_item(text, kind, date, title, url, source, rating):
    """One normalized entry for the search index."""
    return {
        "kind": kind,          # news | analytics | repo
        "date": date,
        "title": title,
        "url": url,
        "source": source,
        "rating": rating,
        "text": text,          # searchable body (bullets/one-liner/etc.)
    }


def build_search_index(page_date, news, analytics_blogs, repos):
    """Append this day's items (news + analytics + repos) to the search index,
    deduped by (kind, url). Returns the items added this run. Non-fatal: on
    any failure the index is left as-is and search falls back to keyword-only."""
    items = []
    for cat in news:
        for it in cat["items"]:
            body = " ".join(it.get("bullets") or [])
            items.append(search_item(
                body or it.get("title", ""), "news", page_date,
                it.get("title", ""), it.get("url", ""),
                it.get("source", ""), it.get("rating", 0)))
    for blog in analytics_blogs:
        for p in blog.get("posts", []):
            body = " ".join(p.get("bullets") or [])
            items.append(search_item(
                body or p.get("title", ""), "analytics", page_date,
                p.get("title", ""), p.get("link", ""),
                p.get("blog", ""), p.get("rating", 0)))
    for r in repos:
        body = " ".join(r.get("bullets") or []) + " " + (r.get("one_liner") or "")
        items.append(search_item(
            body.strip() or r.get("name", ""), "repo", page_date,
            r.get("name", ""), r.get("url", ""),
            r.get("language", ""), 0))
    if not items:
        return []

    # Load existing index, dedupe by (kind, url).
    existing = []
    try:
        data = json.loads(SEARCH_INDEX_PATH.read_text(encoding="utf-8"))
        existing = data.get("items", []) if isinstance(data, dict) else []
    except Exception:
        existing = []
    seen = {(i.get("kind"), i.get("url")) for i in existing}
    new_items = [i for i in items if (i.get("kind"), i.get("url")) not in seen]

    # Embed anything lacking an embedding OR carrying an embedding from a
    # previous embed format (version bump forces a full re-embed, so semantic
    # search always uses the current input format).
    need_reembed = existing and existing[0].get("embed_ver") != SEARCH_INDEX_VERSION
    to_embed = [i for i in new_items] + [
        i for i in existing if not i.get("embedding") or need_reembed]
    for i in to_embed:
        i["embedding"] = None
        i["embed_ver"] = SEARCH_INDEX_VERSION
    if to_embed:
        try:
            texts = [f"{i['title']}\n{i['text']}\nSource: {i.get('source','')}"
                     for i in to_embed]
            vectors = embed_texts(texts)
            for i, v in zip(to_embed, vectors):
                if v:
                    i["embedding"] = v
            log.info("Search: embedded %d item(s) (%d new, %d re-embedded)",
                     len(to_embed), len(new_items),
                     len(to_embed) - len(new_items))
        except Exception as exc:
            log.warning("Search embeddings failed (%s); keyword-only index", exc)
    else:
        log.info("Search: all %d item(s) already embedded", len(existing))

    all_items = existing + new_items
    # Bound the index (cap ~5 years of daily data).
    if len(all_items) > 50000:
        all_items = all_items[-50000:]
    payload = {"version": SEARCH_INDEX_VERSION, "items": all_items}
    SEARCH_INDEX_PATH.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s (%d items)", SEARCH_INDEX_PATH, len(all_items))
    return new_items


# --- Analytics: AI summaries (3-5 bullets per post) -------------------------

ANALYTICS_PROMPT = """You are a technology and finance analyst. Below are new posts from the blog "{blog}". For EACH post write an AI digest of exactly 3 to 5 bullets (never more than 5, never fewer than 3).

The bullets MUST be digested from the provided content: capture key facts, numbers, and implications. NEVER merely rephrase the headline. Each bullet should be one to two full sentences, substantive but tight.

Also assign each post an importance rating from 1 (minor) to 5 (major) — how significant is this for a finance/tech professional.

Return STRICT JSON only: an array of exactly {n} objects, each with keys:
"key" (the post's dedup key, verbatim), "rating" (integer 1-5), "bullets" (array of 3-5 bullet strings).

Posts:
{posts}
"""


def gemini_summarize_analytics(blog_name, posts):
    """One batched Gemini call for a blog's new posts. Raises on failure."""
    lines = []
    for i, post in enumerate(posts, 1):
        content = (post.get("content") or post.get("summary") or "")[:2500]
        lines.append(
            f"{i}. KEY: {post['_key']}\n"
            f"   Title: {post['title']}\n"
            f"   Link: {post['link']}\n"
            f"   Content: {content}"
        )
    prompt = ANALYTICS_PROMPT.format(blog=blog_name, n=len(posts),
                                     posts="\n".join(lines))
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json",
                             "temperature": 0.2},
    }
    data = gemini_generate(payload)
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("Gemini analytics response is not an array")
    by_key = {}
    for entry in parsed:
        bullets = entry.get("bullets") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        bullets = [str(b) for b in bullets if str(b).strip()][:5]
        try:
            rating = max(1, min(5, int(entry.get("rating", 3))))
        except (TypeError, ValueError):
            rating = 3
        by_key[str(entry.get("key", ""))] = (bullets, rating)
    out = []
    for post in posts:
        got = by_key.get(post["_key"])
        if not got or len(got[0]) < 3:
            raise ValueError(f"Gemini missing 3-5 bullets for {post['_key']}")
        post["bullets"] = got[0]
        post["rating"] = got[1]
        out.append(post)
    return out


def heuristic_summarize_analytics(blog_name, posts):
    """Fallback: 3-5 bullets per post from the best available text, rated
    by the keyword heuristic."""
    for post in posts:
        text = post.get("content") or post.get("summary") or post["title"]
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        bullets = []
        for s in sentences:
            s = s.strip()
            if len(s) < 20:
                continue
            if len(s) > 220:
                s = s[:217].rsplit(" ", 1)[0] + "..."
            bullets.append(s)
            if len(bullets) >= 5:
                break
        while len(bullets) < 3:
            bullets.append("Summary from feed preview (full text unavailable).")
        post["bullets"] = bullets[:5]
        post["rating"] = heuristic_rating(
            post["title"] + " " + (post.get("content") or post.get("summary") or ""))
    return posts


def fetch_analytics():
    """Scan all configured blogs; capture posts published within the last 24h,
    rank them by importance, and show at most ANALYTICS_DAILY_MAX per day.

    Selection rule (per user spec):
    - If 10 blogs update, only the top 5 most important posts are shown.
    - Posts not shown are carried forward in a backlog (7-day expiry).
    - If only 3 blogs update today, the remaining 2 slots are filled from the
      backlog (by importance), so the day still shows up to 5.
    - Zero updates and an empty backlog -> the page shows nothing.

    Returns a list of {name, status, posts} — one entry per blog that has a
    selected post, ordered by the highest-rated post. Non-fatal: failures
    degrade per blog."""
    sources = load_analytics_sources()
    if not sources:
        log.warning("Analytics: no sources configured")
        return []
    cutoff = time.time() - MAX_NEWS_AGE_SECONDS
    backlog = load_analytics_backlog()
    backlog_keys = {p.get("_key") for p in backlog if p.get("_key")}

    # Discover feeds (sequential; cheap).
    feed_urls = {}
    for name, url in sources:
        feed = discover_feed(url)
        if feed:
            feed_urls[name] = feed
            log.info("Analytics %s: feed %s", name, feed)
        else:
            log.warning("Analytics %s: no feed discovered for %s", name, url)

    # Fetch entries concurrently.
    entries_by_blog = {}
    with ThreadPoolExecutor(max_workers=ANALYTICS_FETCH_WORKERS) as pool:
        futures = {pool.submit(fetch_feed_entries, u): n
                   for n, u in feed_urls.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                entries_by_blog[name] = fut.result()
            except Exception as exc:
                log.warning("Analytics %s: fetch failed: %s", name, exc)
                entries_by_blog[name] = []

    # Keep only posts published within the last 24h (title + link required —
    # the promise is "every post always has a URL"). Posts already sitting in
    # the backlog are skipped (they were captured before and will be shown
    # when a slot opens).
    fresh_by_blog = {}
    for name, entries in entries_by_blog.items():
        fresh = []
        for entry in entries:
            if not entry.get("_ts") or entry["_ts"] < cutoff:
                continue
            if not entry.get("title") or not entry.get("link"):
                log.warning("Analytics %s: skipping entry without title/link",
                            name)
                continue
            entry["_key"] = analytics_post_key(name, entry)
            if entry["_key"] in backlog_keys:
                continue
            fresh.append(entry)
        if fresh:
            fresh.sort(key=lambda p: p["_ts"], reverse=True)
            fresh_by_blog[name] = fresh

    # Global cap: only the freshest MAX_NEW_TOTAL posts get summarized/rated.
    total_fresh = sum(len(v) for v in fresh_by_blog.values())
    if total_fresh > ANALYTICS_MAX_NEW_TOTAL:
        flat = sorted(
            ((n, p) for n, v in fresh_by_blog.items() for p in v),
            key=lambda t: t[1]["_ts"], reverse=True)
        keep = {}
        for name, p in flat[:ANALYTICS_MAX_NEW_TOTAL]:
            keep.setdefault(name, []).append(p)
        fresh_by_blog = keep

    # Bound per-blog input size for the AI call (token-budget safeguard):
    # only the most recent MAX_PER_BLOG_CALL posts per blog get summarized.
    for name in list(fresh_by_blog):
        if len(fresh_by_blog[name]) > ANALYTICS_MAX_PER_BLOG_CALL:
            fresh_by_blog[name] = fresh_by_blog[name][:ANALYTICS_MAX_PER_BLOG_CALL]

    # Extract full article text for the top few fresh posts per blog.
    targets = []
    for name, posts in fresh_by_blog.items():
        for post in posts[:ANALYTICS_EXTRACT_TOP_N]:
            post["content"] = None
            if post["link"]:
                targets.append((name, post))
    with ThreadPoolExecutor(max_workers=ARTICLE_EXTRACT_WORKERS) as pool:
        futures = {pool.submit(extract_article_text, p["link"]): (n, p)
                   for n, p in targets}
        for fut in as_completed(futures):
            _, post = futures[fut]
            try:
                post["content"] = fut.result()
            except Exception:
                post["content"] = None

    # Summarize + rate per blog (one Gemini call per blog with fresh posts).
    all_fresh = []
    for name, posts in fresh_by_blog.items():
        if not posts:
            continue
        try:
            if GEMINI_API_KEY:
                try:
                    posts = gemini_summarize_analytics(name, posts)
                except Exception as exc:
                    log.warning("Gemini analytics failed for %s (%s); heuristic",
                                name, exc)
                    posts = heuristic_summarize_analytics(name, posts)
            else:
                posts = heuristic_summarize_analytics(name, posts)
        except Exception as exc:
            log.warning("Analytics summary failed for %s: %s", name, exc)
            posts = heuristic_summarize_analytics(name, posts)
        for post in posts:
            post["preview"] = not bool(post.get("content"))
            post["blog"] = name
            all_fresh.append(_sanitize_post(post))
        log.info("Analytics %s: %d post(s) within 24h (%d preview-only)",
                 name, len(posts), sum(1 for p in posts if p.get("preview")))

    # Rank: fresh by importance desc (tie: recency desc), then backlog by
    # importance desc (tie: oldest first, so nothing waits forever).
    all_fresh.sort(key=lambda p: (-(p.get("rating") or 0), -(p.get("_ts") or 0)))
    backlog.sort(key=lambda p: (-(p.get("rating") or 0), (p.get("_ts") or 0)))

    # Select up to the daily max: fresh first, backlog fills the rest.
    selected = all_fresh[:ANALYTICS_DAILY_MAX]
    room = ANALYTICS_DAILY_MAX - len(selected)
    if room > 0:
        selected += backlog[:room]

    # New backlog = old backlog minus shown/expired + fresh not shown.
    shown_keys = {p.get("_key") for p in selected}
    old_cutoff = time.time() - ANALYTICS_BACKLOG_MAX_AGE_DAYS * 86400
    new_backlog = [
        p for p in backlog
        if p.get("_key") not in shown_keys and (p.get("_ts") or 0) >= old_cutoff
    ]
    fresh_selected = {p["_key"] for p in selected if (p.get("_ts") or 0) >= cutoff}
    new_backlog += [p for p in all_fresh if p.get("_key") not in fresh_selected]
    save_analytics_backlog(new_backlog)

    n_fresh = sum(1 for p in selected if (p.get("_ts") or 0) >= cutoff)
    n_carried = len(selected) - n_fresh
    log.info("Analytics: showing %d post(s) (%d fresh + %d carried from "
             "backlog; %d in backlog)",
             len(selected), n_fresh, n_carried, len(new_backlog))

    # Group selected posts by blog, blogs ordered by their best-rated post.
    by_blog = {}
    for post in selected:
        by_blog.setdefault(post["blog"], []).append(post)
    blogs_meta = [
        {"name": name, "status": "ok",
         "posts": sorted(by_blog[name], key=lambda p: -(p.get("rating") or 0))}
        for name in sorted(by_blog,
                           key=lambda n: -max(p.get("rating", 0) for p in by_blog[n]))
    ]
    return blogs_meta


# --- Repo Radar: GitHub trending candidates ---------------------------------

def load_repo_topics():
    """Parse repo_radar.md '## Topics' section -> [(name, query)]. Falls back
    to defaults when the section is missing/empty or the file is absent."""
    try:
        lines = REPO_RADAR_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("Repo radar config missing (%s); using default topics", exc)
        return list(REPO_RADAR_DEFAULT_TOPICS)
    topics = []
    in_section = False
    for line in lines:
        s = line.strip()
        if s.lower().startswith("## topics"):
            in_section = True
            continue
        if s.startswith("##") and in_section:
            break
        if not in_section:
            continue
        m = re.match(r"^-\s+([^:]+):\s*(.+)$", s)
        if m and m.group(2).strip():
            topics.append((m.group(1).strip(), m.group(2).strip()))
    return topics or list(REPO_RADAR_DEFAULT_TOPICS)


def _gh_headers():
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "daily-market-brief"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    return headers


def fetch_repo_candidates():
    """GitHub Search API per topic -> deduped, star-ranked candidate repos.
    Rate limits (403) are non-fatal — the topic is skipped and the radar may
    run short rather than fail."""
    topics = load_repo_topics()
    created_after = (datetime.now(timezone.utc) - timedelta(days=REPO_RADAR_DAYS)) \
        .strftime("%Y-%m-%d")
    candidates = {}
    for name, query in topics:
        q = f"{query} created:>{created_after} stars:>{REPO_RADAR_MIN_STARS}"
        params = {"q": q, "sort": "stars", "order": "desc",
                  "per_page": REPO_RADAR_PER_TOPIC}
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params=params, headers=_gh_headers(), timeout=HTTP_TIMEOUT)
            if resp.status_code == 403:
                log.warning("GitHub search rate-limited (403) for topic %s",
                            name)
                continue
            resp.raise_for_status()
            items = resp.json().get("items", [])
        except Exception as exc:
            log.warning("GitHub search failed for topic %s: %s", name, exc)
            continue
        for it in items:
            full = it.get("full_name") or ""
            if it.get("fork") or not (it.get("description") or "").strip():
                continue
            candidates[full] = {
                "name": full,
                "url": it.get("html_url", ""),
                "language": it.get("language") or "—",
                "stars": it.get("stargazers_count", 0),
                "description": (it.get("description") or "").strip(),
            }
    ranked = sorted(candidates.values(), key=lambda r: -r["stars"])
    log.info("Repo Radar: %d candidate(s) from %d topic(s)",
             len(ranked), len(topics))
    return ranked


# --- Repo Radar: AI curation -------------------------------------------------

REPO_CURATE_PROMPT = """You are a developer scanning GitHub for interesting NEW repositories (created in the last 30 days). Below are candidate repos. Pick the {k} most interesting for a finance/tech professional — weight toward finance & tech, but open to anything genuinely notable.

For each chosen repo provide:
- "name": the exact full_name (verbatim)
- "one_liner": one sentence on what it is
- "bullets": exactly 3-4 bullets covering TECH STRUCTURE (stack/components), PURPOSE, and USE CASES.

Return STRICT JSON only: an array of exactly {k} objects with keys "name", "one_liner", "bullets".

Candidates:
{candidates}
"""


def heuristic_curate_repos(candidates):
    """Fallback: top N by stars with a generic structure/purpose framing."""
    out = []
    for c in candidates[:REPO_RADAR_PICK]:
        out.append({
            **c,
            "one_liner": c["description"],
            "bullets": [
                f"Tech structure: primary language {c['language']}.",
                "Purpose: new open-source project (see description).",
                "Use case: evaluate in your stack if the description fits.",
            ],
        })
    return out


def curate_repos(candidates):
    """Pick the day's 3 repos with AI summaries (heuristic fallback)."""
    if not candidates:
        return []
    if GEMINI_API_KEY:
        try:
            lines = [f"- {c['name']} ({c['language']}, {c['stars']}★): "
                     f"{c['description'][:200]}"
                     for c in candidates]
            prompt = REPO_CURATE_PROMPT.format(
                k=REPO_RADAR_PICK, candidates="\n".join(lines))
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"response_mime_type": "application/json",
                                     "temperature": 0.2},
            }
            data = gemini_generate(payload)
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("Gemini repo response is not an array")
            by_name = {c["name"]: c for c in candidates}
            out = []
            for entry in parsed:
                base = by_name.get(str(entry.get("name", "")))
                if not base:
                    continue
                bullets = entry.get("bullets") or []
                if isinstance(bullets, str):
                    bullets = [bullets]
                bullets = [str(b) for b in bullets if str(b).strip()][:4]
                if not bullets:
                    continue
                out.append({
                    **base,
                    "one_liner": str(entry.get("one_liner", "")
                                     or base["description"]),
                    "bullets": bullets,
                })
                if len(out) >= REPO_RADAR_PICK:
                    break
            if len(out) >= 2:
                return out
            log.warning("Gemini curated only %d repos; using heuristic",
                        len(out))
        except Exception as exc:
            log.warning("Gemini repo curation failed (%s); heuristic", exc)
    return heuristic_curate_repos(candidates)


# --- Repo Radar: README images ----------------------------------------------

REPO_RADAR_IMG_DIR = DATA_DIR / "repo-radar-img"
REPO_IMG_PER_REPO = 3         # max images embedded per repo card
REPO_IMG_CANDIDATES = 6       # candidate images pulled from the README
REPO_IMG_MAX_WIDTH = 1000     # downscale to this max width (keeps clarity)
REPO_IMG_QUALITY = 80         # JPEG quality
REPO_IMG_MIN_BYTES = 8000     # skip tiny images (usually logos/1px spacers)
REPO_IMG_MAX_SRC_BYTES = 5 * 1024 * 1024  # skip huge sources
_IMG_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp)$", re.I)
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)")


def fetch_repo_readme(full_name):
    """Raw README markdown for a repo via the GitHub API (raw accept header),
    or None. Rate limits are non-fatal."""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{full_name}/readme",
            headers={**_gh_headers(),
                     "Accept": "application/vnd.github.raw+json"},
            timeout=HTTP_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        log.warning("Repo Radar %s: README fetch failed: %s", full_name, exc)
        return None


def extract_readme_images(markdown, full_name):
    """Absolute URLs of the first REPO_IMG_CANDIDATES raster images in the
    README. Relative paths resolve against the repo's raw HEAD, so
    'docs/img.png' and './assets/chart.png' work. SVGs are skipped (logos /
    untrusted content); only png/jpg/gif/webp are returned."""
    urls = []
    for alt, url in _MD_IMG_RE.findall(markdown or ""):
        url = url.strip()
        if not url:
            continue
        if url.lower().startswith("data:"):
            continue
        if _IMG_EXT_RE.search(url):
            if url.startswith("http://") or url.startswith("https://"):
                urls.append((alt.strip(), url))
            else:
                path = url.lstrip("./")
                urls.append((alt.strip(),
                             f"https://raw.githubusercontent.com/"
                             f"{full_name}/HEAD/{path}"))
        if len(urls) >= REPO_IMG_CANDIDATES:
            break
    return urls


def _fetch_and_downscale(image_url):
    """Fetch an image URL; downscale + re-encode as JPEG bytes (RGB).
    Returns (bytes, None) on success, (None, reason) on failure."""
    try:
        resp = requests.get(image_url, timeout=ARTICLE_TIMEOUT,
                            headers={"User-Agent": BROWSER_UA})
        if not resp.ok:
            return None, f"HTTP {resp.status_code}"
        data = resp.content
        if len(data) < REPO_IMG_MIN_BYTES:
            return None, "too small"
        if len(data) > REPO_IMG_MAX_SRC_BYTES:
            return None, "too large"
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        if img.width > REPO_IMG_MAX_WIDTH:
            h = max(1, round(img.height * REPO_IMG_MAX_WIDTH / img.width))
            img = img.resize((REPO_IMG_MAX_WIDTH, h), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=REPO_IMG_QUALITY, optimize=True)
        return out.getvalue(), None
    except Exception as exc:
        return None, str(exc)[:80]


def attach_repo_images(repos, page_date):
    """Attach downscaled README images to the day's curated repos, stored
    under docs/data/repo-radar-img/<date>/. Sets repo['img'] = filename and
    repo['img_alt'] = alt text; failures leave the card text-only."""
    if not repos:
        return repos
    day_dir = REPO_RADAR_IMG_DIR / page_date
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Repo Radar: cannot create image dir: %s", exc)
        return repos
    for repo in repos:
        full_name = repo.get("name", "")
        if not full_name:
            continue
        readme = fetch_repo_readme(full_name)
        candidates = extract_readme_images(readme, full_name) if readme else []
        picked = []
        for alt, img_url in candidates:
            if len(picked) >= REPO_IMG_PER_REPO:
                break
            jpeg, reason = _fetch_and_downscale(img_url)
            if jpeg is None:
                log.info("Repo Radar %s: skip image %s (%s)",
                         full_name, img_url, reason)
                continue
            fname = full_name.replace("/", "_") + f"_{len(picked)}.jpg"
            try:
                (day_dir / fname).write_bytes(jpeg)
            except OSError as exc:
                log.warning("Repo Radar %s: cannot write image: %s",
                            full_name, exc)
                break
            picked.append({"file": fname, "alt": alt or full_name})
        if picked:
            repo["img"] = picked[0]["file"]
            repo["img_alt"] = picked[0]["alt"]
            if len(picked) > 1:
                repo["img2"] = picked[1]["file"]
                repo["img2_alt"] = picked[1]["alt"]
        else:
            log.info("Repo Radar %s: no README images found", full_name)
    return repos


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_pages(template, page_date, generated_at, snapshot, takeaways,
                 news, groups, hero=None, new_count=0, day_importance=None,
                 day_verdict=None, weekly=None, weekly_obj=None):
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
        "hero": hero or [],
        "new_count": new_count,
        "day_importance": day_importance,
        "day_verdict": day_verdict,
        "weekly": weekly_obj,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    DAYS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    # Skip GitHub Pages' Jekyll build (static site; the build was timing out).
    (DOCS_DIR / ".nojekyll").touch(exist_ok=True)

    index_html = tpl.render(**ctx, base=".")
    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")
    log.info("Wrote %s", DOCS_DIR / "index.html")

    day_html = tpl.render(**ctx, base="..")
    day_path = DAYS_DIR / f"{page_date}.html"
    day_path.write_text(day_html, encoding="utf-8")
    log.info("Wrote %s", day_path)

    # Standalone saved-news archive page (static shell; content is
    # client-side from localStorage/Firebase).
    archive_tpl = env.get_template("archive.html.j2")
    archive_html = archive_tpl.render(base=".", generated_at=generated_at,
                                      categories=news)
    (DOCS_DIR / "archive.html").write_text(archive_html, encoding="utf-8")
    log.info("Wrote %s", DOCS_DIR / "archive.html")

    # Markets tab: all indicator charts + figures, loaded client-side from
    # data/indicators.json so every page share one 10y dataset.
    markets_tpl = env.get_template("markets.html.j2")
    markets_html = markets_tpl.render(base=".", generated_at=generated_at,
                                      categories=[], is_markets=True)
    (DOCS_DIR / "markets.html").write_text(markets_html, encoding="utf-8")
    log.info("Wrote %s", DOCS_DIR / "markets.html")

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

    # Raw structured data for debugging. Indicator series are NOT included
    # (they live in data/indicators.json) so day files stay small.
    raw = {
        "date": page_date,
        "generated_at": generated_at,
        "snapshot": snapshot,
        "takeaways": takeaways,
        "day_importance": day_importance,
        "day_verdict": day_verdict,
        "weekly": weekly_obj,
        "categories": news,
        "indicator_groups": indicator_summary(groups),
    }
    data_path = DATA_DIR / f"{page_date}.json"
    data_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    log.info("Wrote %s", data_path)

    write_indicators_json(groups, generated_at)


def render_analytics_page(page_date, generated_at, blogs_meta):
    """Render the Analytics (blog digest) page + raw debug JSON.

    The page shows the cumulative history as collapsible per-day sections
    (today open by default, newest first); today's run is also appended to
    the history store."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    update_analytics_history(page_date, generated_at, blogs_meta)

    history_days = []
    try:
        data = json.loads(ANALYTICS_HISTORY_PATH.read_text(encoding="utf-8"))
        raw_days = data.get("days", []) if isinstance(data, dict) else []
    except Exception:
        raw_days = []
    for d in raw_days:
        posts = d.get("posts") or []
        history_days.append({
            "date": d.get("date", ""),
            "generated_at": d.get("generated_at", ""),
            "blogs": _group_posts_by_blog(posts),
            "post_count": len(posts),
        })

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_PATH.parent)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    html = env.get_template("analytics.html.j2").render(
        base=".", page_date=page_date, generated_at=generated_at,
        days=history_days, daily_max=ANALYTICS_DAILY_MAX, is_analytics=True)
    (DOCS_DIR / "analytics.html").write_text(html, encoding="utf-8")
    log.info("Wrote %s", DOCS_DIR / "analytics.html")
    payload = {"generated_at": generated_at, "blogs": blogs_meta}
    ANALYTICS_DATA_PATH.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s", ANALYTICS_DATA_PATH)


def render_repo_radar_page(page_date, generated_at, repos):
    """Render the Repo Radar page + raw debug JSON.

    Like Analytics: cumulative per-day history as collapsible sections
    (today open by default), and today's run is appended to the history."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    update_repo_radar_history(page_date, generated_at, repos)

    history_days = []
    try:
        data = json.loads(REPO_RADAR_HISTORY_PATH.read_text(encoding="utf-8"))
        raw_days = data.get("days", []) if isinstance(data, dict) else []
    except Exception:
        raw_days = []
    for d in raw_days:
        history_days.append({
            "date": d.get("date", ""),
            "generated_at": d.get("generated_at", ""),
            "repos": d.get("repos") or [],
        })

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_PATH.parent)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    html = env.get_template("repo_radar.html.j2").render(
        base=".", page_date=page_date, generated_at=generated_at,
        days=history_days, is_repo_radar=True)
    (DOCS_DIR / "repo-radar.html").write_text(html, encoding="utf-8")
    log.info("Wrote %s", DOCS_DIR / "repo-radar.html")
    payload = {"generated_at": generated_at, "repos": repos}
    REPO_RADAR_DATA_PATH.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s", REPO_RADAR_DATA_PATH)


def render_search_page(generated_at):
    """Render the Search page (keyword + semantic RAG)."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_PATH.parent)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    html = env.get_template("search.html.j2").render(
        base=".", generated_at=generated_at, is_search=True,
        worker_url=WORKER_URL)
    (DOCS_DIR / "search.html").write_text(html, encoding="utf-8")
    log.info("Wrote %s", DOCS_DIR / "search.html")


# ---------------------------------------------------------------------------

def check_outputs(page_date, generated_at, news, groups):
    """Validate what was generated. Returns a list of problems (empty = ok)."""
    problems = []
    required = [
        DOCS_DIR / "index.html",
        DOCS_DIR / "markets.html",
        DOCS_DIR / "archive.html",
        DOCS_DIR / "analytics.html",
        DOCS_DIR / "repo-radar.html",
        DOCS_DIR / "search.html",
        DATA_DIR / f"{page_date}.json",
        DATA_DIR / "indicators.json",
        DATA_DIR / "analytics.json",
        DATA_DIR / "repo_radar.json",
    ]
    for p in required:
        if not p.exists() or p.stat().st_size == 0:
            problems.append(f"missing or empty output: {p.name}")

    # Per-day JSON structure.
    try:
        raw = json.loads((DATA_DIR / f"{page_date}.json")
                         .read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f"day json unreadable: {exc}")
    else:
        if raw.get("generated_at") != generated_at:
            problems.append("day json generated_at mismatch")
        if not raw.get("categories"):
            problems.append("day json has no categories")
        for cat in raw.get("categories") or []:
            # A category MAY have no items: with a hard 24h cutoff, running
            # short is the intended behavior when nothing fresh exists.
            if not isinstance(cat.get("items"), list):
                problems.append(f"category '{cat.get('id')}' items not a list")
        if not isinstance(raw.get("takeaways"), list) or not raw["takeaways"]:
            problems.append("no takeaways generated")
        if not raw.get("snapshot"):
            problems.append("snapshot empty")

    # indicators.json completeness.
    try:
        ind = json.loads((DATA_DIR / "indicators.json")
                         .read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f"indicators json unreadable: {exc}")
    else:
        have = set()
        for gr in ind.get("groups") or []:
            for it in gr.get("items") or []:
                have.add(it.get("label"))
        for gr in INDICATOR_GROUPS:
            for item in gr["items"]:
                if item["label"] not in have:
                    problems.append(f"missing indicator: {item['label']}")

    # AI coverage: with a key set, a fully-heuristic run is a systemic
    # failure (retired model, wrong key, location block) — fail loudly rather
    # than silently publishing a degraded brief.
    if GEMINI_API_KEY and news and AI_SUMMARIES["ok"] == 0:
        problems.append(
            "no AI summaries at all despite GEMINI_API_KEY "
            f"({AI_SUMMARIES['heuristic']} categories heuristic) — "
            "systemic Gemini failure")

    # Analytics + Repo Radar pages must be structurally valid when present;
    # empty source sets are fine (pages render with an empty-note).
    try:
        ana = json.loads(ANALYTICS_DATA_PATH.read_text(encoding="utf-8"))
        if not isinstance(ana.get("blogs"), list):
            problems.append("analytics.json blogs not a list")
    except Exception as exc:
        problems.append(f"analytics json unreadable: {exc}")
    try:
        hist = json.loads(ANALYTICS_HISTORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(hist.get("days"), list):
            problems.append("analytics history days not a list")
        # today's run must have written its entry
        elif not any(d.get("date") == page_date for d in hist["days"]):
            problems.append(f"analytics history missing entry for {page_date}")
    except Exception as exc:
        problems.append(f"analytics history unreadable: {exc}")
    try:
        rr = json.loads(REPO_RADAR_DATA_PATH.read_text(encoding="utf-8"))
        if not isinstance(rr.get("repos"), list):
            problems.append("repo_radar.json repos not a list")
    except Exception as exc:
        problems.append(f"repo radar json unreadable: {exc}")
    try:
        rrh = json.loads(REPO_RADAR_HISTORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(rrh.get("days"), list):
            problems.append("repo radar history days not a list")
        elif not any(d.get("date") == page_date for d in rrh["days"]):
            problems.append(f"repo radar history missing entry for {page_date}")
    except Exception as exc:
        problems.append(f"repo radar history unreadable: {exc}")
    try:
        si = json.loads(SEARCH_INDEX_PATH.read_text(encoding="utf-8"))
        if not isinstance(si.get("items"), list):
            problems.append("search index items not a list")
    except Exception as exc:
        problems.append(f"search index unreadable: {exc}")
    return problems


def print_summary(page_date, generated_at, news, takeaways, groups, snapshot,
                  problems, analytics_blogs=None, repos=None):
    """Console + GitHub Actions step-summary report of the run."""
    ind_ok = sum(1 for gr in groups for it in gr["items"]
                 if it["last"] is not None)
    ind_total = sum(len(gr["items"]) for gr in groups)
    ok, heur = AI_SUMMARIES["ok"], AI_SUMMARIES["heuristic"]
    ai_line = f"{ok}/{ok + heur} AI" + (f", {heur} heuristic" if heur else "")
    analytics_posts = sum(len(b.get("posts", [])) for b in (analytics_blogs or []))
    lines = [
        f"## Daily Market Brief — {page_date}",
        "",
        f"- **Generated:** {generated_at}",
        f"- **AI summaries:** {ai_line}",
        f"- **Takeaways:** {len(takeaways)}",
        f"- **Indicators:** {ind_ok}/{ind_total} ok",
        f"- **Snapshot items:** {len(snapshot)}",
        f"- **Analytics:** {analytics_posts} new post(s) "
        f"from {len(analytics_blogs or [])} blog(s)",
        f"- **Repo Radar:** {len(repos or [])} repo(s)",
        f"- **Check:** {'FAILED' if problems else 'PASS'}",
    ]
    if problems:
        lines.append("")
        lines.append("Problems:")
        lines += [f"  - {p}" for p in problems]
    text = "\n".join(lines)
    print("\n===== RUN SUMMARY =====")
    print(text)
    print("========================")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception as exc:
            log.warning("Could not write step summary: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="Generate the Daily Market Brief site.")
    parser.add_argument("--check", action="store_true",
                        help="validate outputs and exit non-zero on problems")
    args = parser.parse_args()

    now_hk = datetime.now(HK_TZ)
    page_date = now_hk.strftime("%Y-%m-%d")
    generated_at = now_hk.strftime("%Y-%m-%d %H:%M HKT")
    log.info("Daily Market Brief for %s (Gemini: %s)", page_date,
             f"enabled, {gemini_provider()} provider" if GEMINI_API_KEY
             else "disabled, heuristic fallback")

    news = []
    # Cross-day dedup: titles from the previous days' briefs are "claimed"
    # up front, so repeat stories rank below fresh ones in every category.
    claimed_titles = load_recent_titles(page_date)
    # Yesterday's titles only — for the "NEW" tag on first-appearance stories.
    yesterday_titles = load_day_titles(page_date, offset=1)
    new_count = 0
    now = time.time()
    for category in CATEGORIES:
        top_n = category.get("top_n", DEFAULT_TOP_N)
        candidates = fetch_category_news(category)
        candidates, stats = prepare_candidates(candidates, claimed_titles, now)
        # Hard-drop repeat stories (earlier category today, or a previous
        # day's brief) whenever enough unique candidates remain to fill the
        # slots — a repeat only survives as the absolute last resort.
        unique_candidates = [c for c in candidates
                             if not (c["_key"] and c["_key"] in claimed_titles)]
        if len(unique_candidates) >= top_n:
            candidates = unique_candidates
        log.info(
            "%s: %d candidates within 24h (%d stale/undated dropped, "
            "tier1/2/3: %d/%d/%d); "
            "%d cross-category dupe(s) demoted, %d within-category dupe(s) dropped",
            category["label"], stats["candidates"], stats["stale_dropped"],
            stats["tier1"], stats["tier2"], stats["tier3"],
            stats["cross_dupes"], stats["within_dropped"])
        attach_article_texts(category, candidates)
        items = summarize_category(category, candidates)
        for item in items:
            key = normalize_title(item["title"])
            item["is_new"] = bool(key) and key not in yesterday_titles
            if item["is_new"]:
                new_count += 1
            if key:
                claimed_titles.add(key)
        # All selected items are within the 24h window by construction.
        log.info("%s: selected %d/%d items (all within 24h)",
                 category["label"], len(items), top_n)
        if len(items) < top_n:
            log.warning("%s: only %d of %d slots filled "
                        "(no more news within the 24h window)",
                        category["label"], len(items), top_n)
        news.append({
            "id": category["id"],
            "label": category["label"],
            "items": items,
        })

    takeaways = build_takeaways(news)
    log.info("Takeaways: %d bullets", len(takeaways))

    groups = fetch_indicators()
    snapshot = build_snapshot(groups)
    log.info("Snapshot: %s", " · ".join(t["text"] for t in snapshot)
             if snapshot else "(empty)")

    day_importance, day_verdict = build_day_importance(news, groups)
    log.info("Day importance: %d/5 — %s", day_importance, day_verdict)

    weekly = build_weekly(page_date, load_week_days(page_date))
    if weekly:
        log.info("Weekly 5: %d items (%s)", len(weekly["items"]),
                 weekly["week"])

    hero = build_hero(groups, load_day_indicators(page_date, offset=1))
    log.info("Hero: %d cells; %d new story(ies)", len(hero), new_count)

    # ---- Analytics (blog digest) — non-fatal -----------------------------
    analytics_blogs = []
    try:
        analytics_blogs = fetch_analytics()
        log.info("Analytics: %d blog(s) with posts in the last 24h",
                 len(analytics_blogs))
    except Exception:
        log.exception("Analytics failed; continuing without it")

    # ---- Repo Radar — non-fatal ------------------------------------------
    repos = []
    try:
        repos = curate_repos(fetch_repo_candidates())
        log.info("Repo Radar: %d repo(s) curated", len(repos))
        attach_repo_images(repos, page_date)
    except Exception:
        log.exception("Repo Radar failed; continuing without it")

    # ---- Search index (keyword + semantic) — non-fatal -------------------
    try:
        build_search_index(page_date, news, analytics_blogs, repos)
    except Exception:
        log.exception("Search index build failed; continuing")

    render_pages("dashboard.html.j2", page_date, generated_at, snapshot,
                 takeaways, news, groups, hero=hero, new_count=new_count,
                 day_importance=day_importance, day_verdict=day_verdict,
                 weekly_obj=weekly)
    render_analytics_page(page_date, generated_at, analytics_blogs)
    render_repo_radar_page(page_date, generated_at, repos)
    render_search_page(generated_at)

    problems = check_outputs(page_date, generated_at, news, groups)
    print_summary(page_date, generated_at, news, takeaways, groups, snapshot,
                  problems, analytics_blogs, repos)
    if problems:
        for p in problems:
            log.error("CHECK FAIL: %s", p)
        if args.check:
            return 1
    log.info("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Unrecoverable error")
        sys.exit(1)
