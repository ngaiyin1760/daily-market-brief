# Repo Radar configuration

The Repo Radar tab picks 3 interesting/trendy GitHub repositories each day
(finance/tech weighted, open to anything), and writes an AI summary per repo:
what it is / tech structure / purpose / use cases — always with the repo URL,
language, and star count.

## How to edit

- The `## Topics` section drives the daily GitHub search: `- <name>: <query>`.
- The query is keywords only (the generator appends `created:>30d ago` and
  `stars:>50` filters automatically, then sorts by stars).
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

GitHub's trending page has no official API, so the generator approximates it with
the GitHub Search API: repositories created in the last ~30 days, with more than
~50 stars, sorted by stars. Not a perfect proxy for star velocity, but it reliably
surfaces interesting new repos. Finance/tech weighting happens in the AI curation
step (it picks the 5 most interesting from the candidate pool).
