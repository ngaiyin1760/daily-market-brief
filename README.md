# Daily Market Brief

A self-contained static dashboard that gives you a daily snapshot of markets and
news: key indicators (equity indices, bond yields, FX, commodities, VIX, BTC)
plus the top stories — with bullet summaries and 1–5 importance ratings —
across 11 finance categories (global macro, equities, fixed income, China,
Hong Kong, tech, AI, semis, EVs, data centers, space & aerospace).
Top 3 for Global Economy, top 2 for each other category, deduplicated across
categories so each story appears exactly once. Only stories published within
the past 24 hours are ever shown — a category runs short rather than showing
stale news.

Daily pages are all about the news: a snapshot ticker of the day's key moves
(with 1-month trend arrows), a **Day importance banner** (1–5★ telling you at a
glance how hard to read today, with a one-line verdict), the takeaways, and —
on Fridays — **"The Week in 5"** (the 5 things that mattered this week), then
the stories with color-coded importance chips and an importance filter. All
indicator charts and figures live on a separate **Markets** tab — up to 10
years of daily history per instrument, per-chart zoom buttons (1M / 3M / 1Y /
5Y / 10Y), YTD/1M/1Y performance chips, and per-group "as of" freshness
stamps. Run `python generate.py --check` to validate outputs (used by CI to
fail loudly on structural problems or a fully-heuristic run).

Two more always-visible tabs sit under **Markets**:
- **Analytics** — each day it scans every blog in `analytics_sources.md`,
  captures posts published in the last 24 hours, and shows at most **5 posts
  per day ranked by importance** (AI-rated). If more than 5 blogs update, only
  the top 5 are shown and the rest are carried forward in a 7-day backlog that
  fills in on quiet days (e.g. 3 fresh updates + 2 carried = 5). Can be zero.
  The page is a **collapsible day history** (today open, older days collapse
  with the new day on top; a quick "Jump to" menu opens any day). Every post
  has a bookmark button that saves to the same Archive tab as the news.
  Grouped by blog, tech & engineering scope, every post links to its source.
  Every day's shown posts are also appended to a cumulative history
  (`docs/data/analytics_history.json` — newest first, one entry per date) so
  nothing is lost for later use (analysis, RAG, etc.).
- **Repo Radar** — three interesting/trendy GitHub repos picked daily
  (finance/tech weighted, open to anything), each with an AI summary of what
  it is / tech structure / purpose / use cases plus repo URL, language, stars,
  and — when the repo's README has one — an embedded screenshot/diagram/chart
  (downscaled, stored under `docs/data/repo-radar-img/`). Same collapsible
  day history + "Jump to" menu, bookmark buttons that save to Archive, and a
  cumulative history (`docs/data/repo_radar_history.json`). Configure the
  search topics in `repo_radar.md`.

Both tabs follow the site's non-fatal rule: a source that fails to load just
runs short or shows an empty note — it never breaks the daily brief.

The site is a set of static HTML files in `docs/`, hosted free on GitHub Pages
and regenerated daily by GitHub Actions at 7:00 AM Hong Kong time. News
summaries use the Google Gemini API (free tier); without a key the generator
falls back to a built-in heuristic so the run never hard-fails.

## Setup

1. **Get a free Gemini API key.** Two kinds work — `generate.py` auto-detects
   from the key format:
   - **AI Studio key** (starts with `AIza`) from <https://aistudio.google.com/apikey>.
   - **Google Cloud Vertex AI "express mode" key** (starts with `AQ.`) — use this
     if AI Studio isn't available in your region (e.g. Hong Kong). Create one in
     Google Cloud console (Vertex AI express mode); calls go to
     `aiplatform.googleapis.com` with the key in the `x-goog-api-key` header.
   Set `GEMINI_PROVIDER=vertex|aistudio` only if you ever need to override
   auto-detection.
2. **Create a public GitHub repo** named `daily-market-brief` and push this code.
3. **Add the secret**: repo Settings → Secrets and variables → Actions →
   New repository secret → name `GEMINI_API_KEY`, paste the key.
   (Optional — the workflow still succeeds without it, using heuristic summaries.)
4. **Enable Pages**: Settings → Pages → Build and deployment → Deploy from a
   branch → branch `main`, folder `/docs`.
5. **Run it once manually**: Actions → Daily Market Brief → Run workflow.
   After that it runs automatically every day at 7:00 AM HKT (23:00 UTC).

The site will be public at `https://<your-username>.github.io/daily-market-brief/`.

## Local run

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
python generate.py
```

Then open `docs/index.html` in a browser. Set `GEMINI_API_KEY` in the
environment first if you want AI summaries locally; `GEMINI_MODEL` overrides
the model (default `gemini-3.1-flash-lite`).

## How it works

- `generate.py` fetches Google News RSS per category, picks/rates the top N
  stories (3 for Global Economy, 2 for other categories; Gemini or heuristic
  fallback per category on any failure; cross-category title dedup so each
  story appears once; a hard 24-hour age cutoff so nothing stale is ever
  shown). Sources are ranked by tier — Tier 1 → 2 → 3 → unranked — and tier
  outranks timeliness (see `NEWS_SOURCES.md`), pulls
  up to 10 years of daily closes per ticker via yfinance (US 2Y yield from
  FRED), and renders `templates/dashboard.html.j2` into `docs/`.
  It also scans the blogs in `analytics_sources.md` (feed auto-discovery +
  article-text extraction; captures only posts published in the last 24 hours
  and summarizes them with AI, 3–5 bullets per post, grouped by blog), and
  picks 3 trending new GitHub repos per the topics in `repo_radar.md`
  (GitHub Search API, one Gemini curation call), rendering both
  `analytics.html` and `repo-radar.html`.
- Output: `docs/index.html` (latest day), `docs/days/YYYY-MM-DD.html` (one
  standalone page per day), `docs/markets.html` (all indicator charts +
  figures with 10y history and zoom buttons, driven by
  `docs/data/indicators.json`), `docs/archive.html` (saved stories),
  `docs/manifest.json` (date list used by the sidebar on every page, so old
  pages automatically see new days), and `docs/data/YYYY-MM-DD.json` (raw
  structured data for debugging; indicator closes live only in
  `data/indicators.json`).
- Indicator fetch failures (e.g. yfinance rate-limiting) are non-fatal and
  render as `n/a`.

## Cross-device sync (optional)

Saved news (`dmb_saved`) and per-day read status (`dmb_read`) live in browser
localStorage by default — per device. The site also includes an optional
Firebase sync: click **Sign in to sync** in the Saved section header (Google
sign-in) and both maps sync via Firestore between phone and laptop, with
realtime updates. Everything is client-side; if the Firebase CDN is blocked
or you're signed out, the page works exactly as before with localStorage only.
Sign-out keeps local data.

To enable it for your own deployment, create a Firebase project with
Firestore (production mode) and the Google sign-in provider, add your Pages
domain as an authorized domain, replace the config in
`templates/dashboard.html.j2` (`FIREBASE_CONFIG`), and set these Firestore
security rules (Firebase console → Build → Firestore Database → Rules) so
each user can only access their own document:

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{userId} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
  }
}
```

Note: the generated site and everything it displays is public once Pages is
enabled. Don't put private data in the repo.
