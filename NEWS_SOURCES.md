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
