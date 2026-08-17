# Repo Radar configuration

The Repo Radar tab picks 5 interesting/trendy GitHub repositories each day
(finance/tech weighted, open to anything), and writes an AI summary per repo:
what it is / tech structure / purpose / use cases — always with the repo URL,
language, and star count.

## How to edit

- The `## Topics` section drives both topic-priority weighting and the fallback
  GitHub search: `- <name>: <query>`.
- The query is keywords only (used as-is in the fallback search, and matched
  against repo names/descriptions to prioritize topics you care about).
- Add, remove, or reorder topics freely. Lines not matching `- name: query` are
  ignored.
- If this section is missing or empty, defaults are used (see below).

## Topics

- fintech: fintech OR quant OR trading OR defi
- ai-llm: llm OR ai-agent OR agentic
- infra: infrastructure OR devtools

## Default topics (used if the section above is empty)

- fintech: `fintech OR quant OR trading OR defi`
- ai-llm: `llm OR ai-agent`
- infra: `infrastructure OR devtools`

## How "trendy" is determined

The generator primarily sources candidates from **trendshift.io**, a live
"trending by velocity" ranking that catches repos as they rise (not after they
peak). It scrapes trendshift's daily ranking, enriches each repo with
stars/language/fork via the GitHub API, and excludes repos already shown in
previous days' Repo Radar.

When several candidates are very similar (forks/clones of the same project),
they are collapsed to the one that best matches the topics in `## Topics`
above; ties break by trendshift position, then stars. The AI curation step then
picks the 5 most interesting from the surviving candidates, weighting the
listed topics.

If trendshift is unreachable, the generator falls back to a GitHub Search API
approximation: repositories created in the last ~30 days with more than ~50
stars, sorted by stars.
