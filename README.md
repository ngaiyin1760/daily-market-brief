# Daily Market Brief

A self-contained static dashboard that gives you a daily snapshot of markets and
news: key indicators (equity indices, bond yields, FX, commodities, VIX, BTC)
plus the top 3 stories — with bullet summaries and 1–5 importance ratings —
across 11 finance categories (global macro, equities, fixed income, China,
Hong Kong, tech, AI, semis, EVs, data centers, space & aerospace).

The site is a set of static HTML files in `docs/`, hosted free on GitHub Pages
and regenerated daily by GitHub Actions at 9:00 AM Hong Kong time. News
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
   After that it runs automatically every day at 9:00 AM HKT (01:00 UTC).

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
the model (default `gemini-2.5-flash`).

## How it works

- `generate.py` fetches Google News RSS per category, picks/rates the top 3
  stories (Gemini or heuristic fallback per category on any failure), pulls
  ~1 month of daily closes per ticker via yfinance (US 2Y yield from FRED),
  and renders `templates/dashboard.html.j2` into `docs/`.
- Output: `docs/index.html` (latest day), `docs/days/YYYY-MM-DD.html` (one
  standalone page per day), `docs/manifest.json` (date list used by the
  sidebar on every page, so old pages automatically see new days), and
  `docs/data/YYYY-MM-DD.json` (raw structured data for debugging).
- Indicator fetch failures (e.g. yfinance rate-limiting) are non-fatal and
  render as `n/a`.

Note: the generated site and everything it displays is public once Pages is
enabled. Don't put private data in the repo.
