# News Source Tiers

How the Daily Market Brief ranks news sources. This file is the reference;
the ranking lives in `generate.py` (`TIER_1_SOURCES`, `TIER_2_SOURCES`,
`TIER_3_SOURCES` in the `SOURCE_TIERS` tuple). Keep the two in sync.

## Ranking rule

**Tier overrides timeliness.** Candidates are ordered by:

1. Non-duplicate vs. duplicate story (cross-category / cross-day dedup)
2. **Source tier**: Tier 1 → Tier 2 → Tier 3 → unranked
3. Recency (newest first)

So a Tier 1 story published 23 hours ago beats a Tier 2 story published
minutes ago. Only within the same tier does timeliness decide.

Step 1 has two different rules for the two kinds of duplicate:

- **Same day, earlier category: never repeated.** A story already published
  by an earlier category of the same run is dropped outright, even if that
  leaves the category short — or empty. The brief degrades by running short,
  never by showing the same story twice in one day.
- **Previous 3 days: demoted, last resort.** A story published on a recent
  day sinks to the bottom of the ranking and is used only when nothing else
  can fill the slots.

Sameness is decided on two keys: the normalized title — with a conservative
near-duplicate fallback so a reworded or respelled copy of the same story
still matches — and the article link key, which is the `/articles/<token>`
fragment of a **Google News** link (stable across Google's category feeds,
and the stronger of the two) or, for a link that is not Google's (a
Bing-fallback item), the whole URL. The token is never read off a publisher
URL: those can contain `/articles/<date>/`, and a date read as a token would
mark every story that publisher filed that day as already published.

Within a category, **distinct publishers are preferred**: when two candidates
are of comparable importance, the one from an outlet not already chosen in
that category wins. This is a preference, not a cap — a single outlet may
still fill both slots on a beat it legitimately dominates (Hong Kong → SCMP
has done so for days), and it may never cost a genuinely more important
story.

The feed is fetched with a Google News recency operator (`when:1d` by
default — `DEFAULT_QUERY_WINDOW` in `generate.py`; `ai-research` overrides
it to `when:2d`). Google News ranks by relevance across all time, so a query
without one returns evergreen analysis — economic monitors, magazine essays,
months-old columns — that the 24h cutoff then discards, leaving the category
empty.

Two hard constraints still apply before ranking:

- **24-hour freshness cutoff** — anything older than 24h is dropped
  outright, from any tier. The brief runs a category short rather than
  showing stale news.
- **Never a hard filter** — tiers rank, they don't exclude. When no tiered
  source covers a topic within the window, unranked outlets fill the slot.

When Google News clusters the same story across outlets, the copy from the
highest tier wins (newest copy breaks a tie).

## Matching

Publisher names are matched after lowercasing and stripping punctuation, so
`Bloomberg.com`, `bloomberg`, and `Bloomberg` all match the same entry.
Google News occasionally varies the label it reports, so a few aliases
(e.g. `wsj`, `wsjcom`, `scmp`, `scmpcom`) are listed explicitly.

## Tier 1 — Global wires & top-tier financial press

Highest trust: primary reporting on markets, macro, and global affairs.
Default first pick in every category they cover.

| Normalized key | Display names seen |
|---|---|
| `reuters` / `reuterscom` | Reuters |
| `bloomberg` / `bloombergcom` | Bloomberg, Bloomberg.com |
| `apnews` / `associatedpress` | AP News, Associated Press |
| `financialtimes` / `ftcom` | Financial Times |
| `thewallstreetjournal` / `wsj` / `wsjcom` | The Wall Street Journal, WSJ |
| `cnbc` | CNBC |
| `barrons` | Barron's |
| `marketwatch` | MarketWatch |
| `theeconomist` | The Economist |
| `nikkeiasia` / `asianikkei` / `nikkei` | Nikkei Asia, asia.nikkei.com |

## Tier 2 — Quality business & general press

Solid business desks or credible general coverage. Strong second choice when
Tier 1 hasn't covered the story.

| Normalized key | Display names seen |
|---|---|
| `yahoofinance` / `yahoofinanceuk` | Yahoo Finance, Yahoo Finance UK |
| `investingcom` | Investing.com |
| `fortune` | Fortune |
| `thebusinesstimes` | The Business Times |
| `bbc` / `bbcnews` | BBC |
| `cnn` | CNN |
| `abcnews` / `abcnewscom` | ABC News |
| `cbsnews` | CBS News |
| `npr` / `nprorg` | NPR |
| `theguardian` / `theguardiancom` | The Guardian |
| `thenewyorktimes` / `nytimes` | The New York Times |
| `washingtonpost` | The Washington Post |
| `axios` | Axios |
| `semafor` | Semafor |
| `globalnews` / `globalnewsca` | Global News |
| `financialpost` | Financial Post |
| `theglobeandmail` | The Globe and Mail |
| `thetelegraph` | The Telegraph |
| `straitstimes` | The Straits Times |
| `thejapantimes` | The Japan Times |
| `taipeitimes` | Taipei Times |
| `southchinamorningpost` / `scmp` / `scmpcom` | South China Morning Post |
| `caixinglobal` / `caixin` | Caixin Global, Caixin |
| `hongkongeconomictimes` / `hket` | Hong Kong Economic Times |
| `aastocks` | AASTOCKS |
| `thestandard` | The Standard |

## Tier 3 — Sector trade press & specialized

The right source for a specific beat (AI, semis, EV, space, data centers)
when it carries a story the majors haven't, or with more technical depth.
Also includes a few specialist analysts and state-owned national press.

| Normalized key | Display names seen |
|---|---|
| `techcrunch` | TechCrunch |
| `theinformation` | The Information |
| `venturebeat` | VentureBeat |
| `mittechnologyreview` | MIT Technology Review |
| `tomshardware` | Tom's Hardware |
| `trendforce` | TrendForce |
| `digitimes` | DigiTimes |
| `insideevs` | InsideEVs |
| `cleantechnica` | CleanTechnica |
| `electrek` | Electrek |
| `spacenews` / `spacenewscom` | SpaceNews |
| `spaceflightnow` | Spaceflight Now |
| `nasaspaceflight` / `nasaspaceflightcom` | NASASpaceflight |
| `spacecom` | Space.com |
| `payloadspace` | Payload Space |
| `datacenterdynamics` | Data Center Dynamics |
| `datacenterknowledge` | Data Center Knowledge |
| `chinadaily` / `chinadailyglobaledition` | China Daily |
| `seekingalpha` | Seeking Alpha |
| `foxbusiness` | Fox Business |
| `councilonforeignrelations` | Council on Foreign Relations |

## Unranked

Everything else — picked only when no tiered source covers the topic in the
24h window. Observed in recent briefs (non-exhaustive): The Motley Fool,
24/7 Wall St., finance.biggo.com, Real Estate Asia, Open Magazine, Data
Centre Magazine, The Korea Daily, Global Times, FXStreet, Benzinga,
Stocktwits, Business Insider Africa, VnExpress, and assorted local/regional
papers.

## Editing

- Adding a source: pick its tier above, add the normalized key to the
  matching set in `generate.py`, and add a row here.
- A source can move down if its quality drops — move the key, not the rule.
- Never add content farms or syndication spam; unranked exists for a reason.
- `normalize_title` in `generate.py` is the dedup key: its canonicalization
  (suffix stripping, `&`→`and`, the British→American word list) governs
  which headline variants count as the same story. Widen it and distinct
  stories start colliding; narrow it and reworded copies leak through —
  change it conservatively, and keep the near-duplicate fallback in mind as
  the second line of defence.
