# Analytics Sources (blog registry)

The Analytics tab scans the blogs below daily. It captures **only posts
published in the last 24 hours**, rates them by importance (AI), and shows
**at most 5 posts per day** — the top 5. When more than 5 blogs update, the
leftovers are carried forward in a backlog; when fewer than 5 update, the
remaining slots are filled from that backlog (7-day expiry). Zero updates and
an empty backlog means the page shows nothing that day. Scope: technology &
engineering — finance/economics included, but technical and engineering posts
are equally welcome.

## How to edit

- One source per line: `- Name — URL` (URL may be a direct feed or a page
  feedparser can auto-discover). Lines not starting with `- ` are ignored, so
  comments/blank lines are fine.
- Add, remove, or reorder freely; the generator reads this file every run.
- To *temporarily* disable a source, delete its line (or comment it out with `#`).

## Sources

- CNEVPOST — https://cnevpost.com/feed/
- S&P Global Mobility — https://www.spglobal.com/mobility/en/
- JPM Insights — https://am.jpmorgan.com/hk/en/asset-management/per/insights/
- IEA — https://www.iea.org/
- MIT Technology Review — https://www.technologyreview.com/feed/
- IFP — https://ifp.org/
- CSDN — https://www.csdn.net/
- SEMI Blogs — https://www.semi.org/en/blogs
- SemiEngineering — https://semiengineering.com/feed/
- SIA Blog — https://www.semiconductors.org/category/blog/
- CSIS — https://www.csis.org/rss.xml
- CSET Georgetown — https://cset.georgetown.edu/feed/
- BlackRock GIO — https://www.blackrock.com/sg/en/insights/global-investment-outlook
- American Affairs — https://americanaffairsjournal.org/feed/

## Added beyond the original list (major tech/engineering blogs)

- Stratechery — https://stratechery.com/feed/
- Benedict Evans — https://www.ben-evans.com/benedictevans?format=rss
- Marginal Revolution — https://feeds.feedblitz.com/marginalrevolution
- The Diff — https://www.thediff.co/feed/
- AVC — https://avc.com/feed/

## Feed availability notes (from probing, 2026-08-12)

| Source | Feed | Notes |
|---|---|---|
| CNEVPOST | ✅ direct feed | full-text RSS |
| MIT Technology Review | ✅ direct feed | full-text RSS |
| SemiEngineering | ✅ direct feed | full-text RSS |
| CSIS | ✅ direct feed | full-text RSS |
| CSET Georgetown | ✅ direct feed | full-text RSS |
| American Affairs | ✅ direct feed | full-text RSS |
| Stratechery | ✅ direct feed | — |
| Benedict Evans | ✅ direct feed | — |
| Marginal Revolution | ✅ direct feed | via FeedBlitz (`feeds.feedblitz.com/marginalrevolution`) |
| The Diff | ✅ direct feed | — |
| AVC | ✅ direct feed | — |
| IEA | ⚠️ Cloudflare | homepage + /rss return 403 "Just a moment…" — may fail from the runner too; will tag preview-only or log "no feed" if blocked |
| SEMI Blogs | ⚠️ Drupal page | no confirmed feed; auto-discovery may find one |
| SIA Blog | ⚠️ page | blog feed path served HTML; may need a scrape rule |
| S&P Global Mobility | ⚠️ 403 | blocks non-browser UAs; likely fails from the runner |
| JPM Insights | ⚠️ JS-heavy | no feed detected; needs a scrape rule or drop |
| BlackRock GIO | ⚠️ JS-heavy | no feed detected; needs a scrape rule or drop |
| CSDN | ⚠️ no feed | Chinese dev portal, no public RSS; needs a scrape rule or drop |
| IFP | ⚠️ page | WordPress; likely has /feed/ — auto-discovery will try |

Sources marked ⚠️ are still attempted every run (auto-discovery + article-text
extraction); the generator logs per-source status so we can decide later whether
to add a scrape rule or remove them.
