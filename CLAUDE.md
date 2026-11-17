# CLAUDE.md

Hardware & embedded internship alert bot. Single-file Python app run every 15 min by GitHub Actions.

## Architecture
- `scraper.py` — fetchers (Greenhouse, Lever, Ashby, GitHub README tables) -> `Matcher` (level keyword, exclude words, location, role categories) -> dedupe (`job_id` per posting, `soft_key` across sources) -> `Notifier` (ntfy, optional Discord).
- `config.json` — all user-tunable behaviour (companies, keywords, locations, categories). No code change needed to retarget.
- `seen_jobs.json` — state, committed back by the workflow. First run baselines silently; later runs alert only on new roles.
- `.github/workflows/check_internships.yml` — scheduled scrape; `ci.yml` — pytest on push/PR.

## Commands
- Install: `pip install -r requirements.txt pytest`
- Test: `pytest -q` (offline, mock payloads only)
- Dry run (no notifications, no state written): `python scraper.py` with `NTFY_TOPIC` unset

## Rules
- The ntfy topic lives ONLY in the `NTFY_TOPIC` GitHub secret. Never put it in `config.json` (repo is public; `Notifier` aborts if it sees one in CI).
- `config.json` must stay valid JSON (no comments, no trailing commas).
- Use `logging` (logger `internship_bot`), not `print()`. Never log the topic or webhook; use `Notifier._scrub`.
- Keep type hints and docstrings on new functions; add a test in `tests/test_scraper.py` for matching/dedupe changes.
