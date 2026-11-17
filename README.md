# Hardware & Embedded Internship Alert Bot

[![CI](https://github.com/AndyMuka/hw-internship-alert-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/AndyMuka/hw-internship-alert-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A serverless bot that watches company career boards and community internship trackers for **FPGA, ASIC, embedded, firmware, GPU, HPC and EE/SW-hybrid** roles at large companies and hardware startups, and pushes new matches to your phone. It runs on GitHub Actions and ntfy, so it costs **$0** and needs no server. See [Scheduling](#scheduling) for how often it checks.

## How it works

```
 Greenhouse API ─┐
 Lever API ──────┤
 Ashby API ──────┼─►  scraper.py  ──►  Matcher  ──►  Dedupe  ──►  ntfy.sh  ──►  📱 phone
 GitHub trackers ┘   (fetch+parse)    level kw        job_id        (push)
 (README tables)                      exclude kw      soft_key
                                      location        across sources
                                      role category
                          ▲                               │
                          │                               ▼
  config.json ────────────┘                        seen_jobs.json
  (all tuning)                                  (state, committed back)

  GitHub Actions: cron every 15 min  ──►  runs scraper.py  ──►  commits seen_jobs.json
  Secret NTFY_TOPIC  ──►  injected at runtime (never in the repo)
```

- **Tiered filtering:** a title must contain a level keyword (intern, co-op…), no exclude keyword (senior, new grad…), an allowed location, and at least one enabled role category.
- **Whole-word matching:** `intern` won't match "internal", `soc` won't match "social"; simple plurals still match.
- **No flood on day one:** the first run records existing roles silently and sends one summary. Newly added companies/feeds are baselined the same way.
- **Cross-source dedupe:** the same role listed on a company board and a tracker pings once.
- **Newest first:** alerts are sent in order of posting date, and every run refreshes `MATCHES.md` in a separate **private** repo (via a repo-scoped deploy key stored as the `MATCHES_DEPLOY_KEY` secret), and each alert has an "All matches" button that opens it. Set `notifications.matches_url` in `config.json` to your own list URL.
- **Private by design:** the repo is public (free, unlimited Actions minutes) but your ntfy topic is an encrypted secret and is scrubbed from logs.

## Development workflow (Claude Code)

Built and polished with [Claude Code](https://claude.com/claude-code): the codebase was reviewed, type-annotated and moved to structured `logging` by the agent, and the 30+ offline `pytest` cases (tiered matching, word boundaries, excludes, locations, dedupe, feed and Ashby parsing) were written against mock payloads and run locally before every push. CI re-runs them on each push and PR. [`CLAUDE.md`](CLAUDE.md) records the architecture and rules the agent follows in this repo.

## Quick start

1. Install the **ntfy** app and subscribe to a hard-to-guess topic, e.g. `hw-jobs-andy-48213907` (add random digits; anyone who knows the topic can read it).
2. Fork or clone this repo as a **public** repository.
3. **Settings → Actions → General → Workflow permissions →** *Read and write permissions*.
4. Save your topic as a secret:
   ```bash
   gh secret set NTFY_TOPIC
   ```
5. Run it once:
   ```bash
   gh workflow run check_internships.yml
   ```
   You should get an "Internship bot initialized" notification. After that it runs on the schedule above.

Local dry run (nothing sent, no state written): `pip install -r requirements.txt && python scraper.py`.

## Scheduling

The workflow is set to run every 15 minutes, but GitHub treats `schedule` as best-effort and can delay or skip runs, sometimes for hours. For tighter timing, call the workflow's `workflow_dispatch` endpoint from a free external cron service (a fine-grained token limited to this repo with *Actions: Read and write*). Runs on a public repository are free either way.

## Customizing `config.json`

All behaviour lives in `config.json`; edit it on GitHub or locally and push. No code changes needed.

| Key | What it does |
|---|---|
| `companies.greenhouse` / `companies.lever` / `companies.ashby` | `"board-slug": "Display Name"`. The slug is from the careers URL (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`). Ashby is common among startups. |
| `github_feeds` | Raw README URLs of community trackers; set `"enabled": false` to pause one. |
| `level_keywords` | Title must contain one (intern, co-op, `summer 2027`…). |
| `role_categories` | Each has `enabled`, `label`, `priority` (1–5, sets notification urgency) and `keywords`. Add your own block to track a new field. |
| `exclude_keywords` | Titles containing any of these are dropped. |
| `allowed_locations` | Case-insensitive substrings (`"toronto"`, `", ca"`, `"remote"`). |
| `matching` | `match_whole_words`, `allow_unknown_location`, `dedupe_across_sources`. |
| `notifications` | `max_individual_alerts_per_run`; overflow is sent as one digest. |

**JSON rules:** double quotes only, commas between items, no trailing comma, no comments. A mistake makes the run fail with the line and column.

**Never** put your ntfy topic or a Discord webhook in `config.json`; use the `NTFY_TOPIC` / `DISCORD_WEBHOOK_URL` secrets. The bot refuses to run on a public repo if it finds one there.

## Tests

```bash
pip install -r requirements.txt pytest
pytest -q
```

## License

[MIT](LICENSE)
