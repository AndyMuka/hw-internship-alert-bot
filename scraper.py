#!/usr/bin/env python3
"""Hardware & Embedded Internship Alert Bot.

Fetches jobs from Greenhouse boards, Lever boards and GitHub internship
tracker READMEs, filters them using ``config.json``, and pushes new matches
to your phone through ntfy (and optionally Discord).

Everything you would normally want to change lives in ``config.json``.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen_jobs.json")

PLACEHOLDER_TOPIC = "CHANGE_ME_TO_SECRET_TOPIC"
HTTP_TIMEOUT = 45
USER_AGENT = "internship-alert-bot/1.0 (personal job alert script)"

log = logging.getLogger("internship_bot")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

Job = dict[str, str]
State = dict[str, Any]
Category = dict[str, Any]


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """Load ``config.json``; exit with a friendly message if missing or invalid."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit("ERROR: config.json not found next to scraper.py")
    except json.JSONDecodeError as e:
        sys.exit(
            f"ERROR: config.json is not valid JSON (line {e.lineno}, column "
            f"{e.colno}): {e.msg}\nCommon causes: a missing or trailing comma, "
            "or a missing double quote. See the README section 'JSON rules'."
        )


def load_state() -> tuple[State, bool]:
    """Return ``(state, first_run)``.

    ``first_run`` is True if the state file is missing, empty or corrupt.
    """
    blank: State = {"initialized": False, "sources": [], "seen": {}, "alerted_keys": {}}
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        data = json.loads(raw) if raw else {}
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError:
        log.warning("seen_jobs.json was corrupt; starting fresh (no flood: "
                    "this run is treated as a first run).")
        data = {}
    if not isinstance(data, dict):
        data = {}
    state = dict(blank)
    for key in blank:
        if isinstance(data.get(key), type(blank[key])):
            state[key] = data[key]
    first_run = not state["initialized"] and not state["seen"]
    return state, first_run


def save_state(state: State) -> None:
    """Persist ``state`` to ``seen_jobs.json`` with stable key ordering."""
    state["sources"] = sorted(set(state["sources"]))
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


# --------------------------------------------------------------------------
# Text helpers and matching
# --------------------------------------------------------------------------

def normalize(text: str | None) -> str:
    """Lowercase, unify dashes and collapse whitespace."""
    text = (text or "").lower()
    text = re.sub(r"[‐-―−]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def keyword_pattern(keyword: str, whole_words: bool) -> re.Pattern[str]:
    """Compile a keyword into a regex.

    With ``whole_words`` the match is bounded and tolerant of simple endings:
    ``fpga`` matches "FPGAs", but ``intern`` does NOT match "internal" and
    ``soc`` does NOT match "social".
    """
    kw = re.escape(normalize(keyword))
    if not whole_words:
        return re.compile(kw)
    return re.compile(r"(?<![a-z0-9])" + kw + r"(?:s|es|ing)?(?![a-z0-9])")


class Matcher:
    """Tiered job filter built from ``config.json``.

    A job passes if its title has a level keyword, has no exclude keyword and
    has an allowed location. It is then tagged with every enabled role
    category whose keywords appear in the title or department.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        opts = cfg.get("matching", {})
        whole = bool(opts.get("match_whole_words", True))
        self.allow_unknown_location = bool(opts.get("allow_unknown_location", False))

        def compile_list(words: list[str]) -> list[re.Pattern[str]]:
            return [keyword_pattern(w, whole) for w in words if str(w).strip()]

        self.level = compile_list(cfg.get("level_keywords", []))
        self.exclude = compile_list(cfg.get("exclude_keywords", []))
        # Plain case-insensitive substring match (entries such as ", ca" kept as-is).
        self.locations = [x.lower() for x in cfg.get("allowed_locations", []) if x.strip()]

        self.categories: list[Category] = []
        for key, cat in cfg.get("role_categories", {}).items():
            if not isinstance(cat, dict) or not cat.get("enabled", False):
                continue
            self.categories.append({
                "key": key,
                "label": cat.get("label") or key.replace("_", " ").title(),
                "priority": int(cat.get("priority", 3)),
                "emoji_tag": cat.get("emoji_tag", ""),
                "patterns": compile_list(cat.get("keywords", [])),
            })

    def match(self, job: Job) -> list[Category]:
        """Return the matched categories, or ``[]`` if the job is filtered out."""
        title = normalize(job["title"])
        if not any(p.search(title) for p in self.level):
            return []
        if any(p.search(title) for p in self.exclude):
            return []
        location = (job.get("location") or "").lower()
        if location.strip():
            if not any(loc in location for loc in self.locations):
                return []
        elif not self.allow_unknown_location:
            return []
        haystack = title + " | " + normalize(job.get("department", ""))
        return [c for c in self.categories
                if any(p.search(haystack) for p in c["patterns"])]


def job_id(job: Job) -> str:
    """Stable per-posting id: company + title + URL."""
    raw = "|".join([normalize(job["company"]), normalize(job["title"]), job["url"].strip()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def soft_key(job: Job) -> str:
    """Company + title only: used to avoid pinging the same role from two sources."""
    company = re.sub(r"[^a-z0-9]", "", normalize(job["company"]))
    title = re.sub(r"[^a-z0-9]", "", normalize(job["title"]))
    return hashlib.sha256(f"{company}|{title}".encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------
# Fetchers. Each returns a list of job dicts, or None if the source failed.
# --------------------------------------------------------------------------

def http_get(url: str, **kwargs: Any) -> requests.Response:
    """GET with one retry. A 404 is returned (not raised) so callers can explain it."""
    last_error: requests.RequestException | None = None
    for _ in range(2):
        try:
            resp = SESSION.get(url, timeout=HTTP_TIMEOUT, **kwargs)
            if resp.status_code == 404:
                return resp
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_error = e
            time.sleep(2)
    assert last_error is not None
    raise last_error


def make_job(source: str, company: str, title: str, location: str, url: str,
             department: str = "") -> Job:
    """Build a normalized job dict with all fields stripped strings."""
    return {
        "source": source,
        "company": (company or "").strip(),
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
        "department": (department or "").strip(),
    }


def fetch_greenhouse(slug: str, name: str) -> list[Job] | None:
    """Fetch all jobs from a public Greenhouse board; None on failure."""
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
    try:
        resp = http_get(f"{base}/jobs")
        if resp.status_code == 404:
            log.warning("[greenhouse:%s] 404 - no public board with this slug. "
                        "Check the slug or remove it from config.json.", slug)
            return None
        jobs = resp.json().get("jobs", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("[greenhouse:%s] fetch failed: %s", slug, e)
        return None

    # Department names are optional extra signal; ignore failures.
    departments: dict[Any, str] = {}
    try:
        dresp = http_get(f"{base}/departments")
        if dresp.status_code == 200:
            for dept in dresp.json().get("departments", []):
                for j in dept.get("jobs", []) or []:
                    departments.setdefault(j.get("id"), dept.get("name", ""))
    except (requests.RequestException, ValueError):
        pass

    out: list[Job] = []
    for j in jobs:
        url = j.get("absolute_url") or ""
        title = j.get("title") or ""
        if not url or not title:
            continue
        location = (j.get("location") or {}).get("name", "")
        out.append(make_job(f"greenhouse:{slug}", name, title, location, url,
                            departments.get(j.get("id"), "")))
    return out


def fetch_lever(slug: str, name: str) -> list[Job] | None:
    """Fetch all postings from a public Lever board; None on failure."""
    try:
        resp = http_get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
        if resp.status_code == 404:
            log.warning("[lever:%s] 404 - no public board with this slug. "
                        "Check the slug or remove it from config.json.", slug)
            return None
        postings = resp.json()
        if not isinstance(postings, list):
            raise ValueError("unexpected response shape")
    except (requests.RequestException, ValueError) as e:
        log.warning("[lever:%s] fetch failed: %s", slug, e)
        return None

    out: list[Job] = []
    for p in postings:
        cats = p.get("categories") or {}
        url = p.get("hostedUrl") or ""
        title = p.get("text") or ""
        if not url or not title:
            continue
        locs = list(cats.get("allLocations") or [])
        if cats.get("location") and cats["location"] not in locs:
            locs.insert(0, cats["location"])
        if (p.get("workplaceType") or "").lower() == "remote":
            locs.append("Remote")
        department = " / ".join(x for x in (cats.get("department"), cats.get("team")) if x)
        out.append(make_job(f"lever:{slug}", name, title, "; ".join(locs), url, department))
    return out


# ---- GitHub README tables (HTML tables and Markdown pipe tables) ----------

_TAG_RE = re.compile(r"<[^>]+>")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_SYMBOL_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⬀-⯿️‍]")


def clean_cell(cell: str) -> str:
    """Strip HTML/Markdown markup, emoji and extra whitespace from a table cell."""
    text = re.sub(r"(?i)<\s*/?\s*br\s*/?\s*>", "; ", cell)
    text = re.sub(r"(?i)</summary>", ": ", text)
    text = _TAG_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = html.unescape(text)
    text = text.replace("**", "").replace("`", "")
    text = _SYMBOL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ;")
    return text


def extract_url(cell: str) -> str:
    """Return the best apply URL in a cell, preferring non-aggregator links."""
    urls = re.findall(r'href\s*=\s*"([^"]+)"', cell)
    urls += [m[1] for m in _MD_LINK_RE.findall(cell)]
    urls = [html.unescape(u).strip() for u in urls if u.lower().startswith("http")]
    if not urls:
        return ""
    # Prefer the employer's own link over an aggregator's page.
    for u in urls:
        if "simplify.jobs" not in u:
            return u
    return urls[0]


def column_map(headers: list[str]) -> dict[str, int] | None:
    """Map table headers to company/role/location/link columns, or None if unusable."""
    cols: dict[str, int] = {}
    for i, h in enumerate(h.lower() for h in headers):
        if "company" in h:
            cols.setdefault("company", i)
        elif "role" in h or "position" in h or "title" in h:
            cols.setdefault("role", i)
        elif "location" in h:
            cols.setdefault("location", i)
        elif "application" in h or "link" in h or "apply" in h:
            cols.setdefault("link", i)
    if {"company", "role", "link"} <= set(cols):
        return cols
    return None


def rows_to_jobs(source: str, rows: list[list[str]], cols: dict[str, int]) -> list[Job]:
    """Turn raw table rows into jobs; ``↳`` rows inherit the previous company."""
    jobs: list[Job] = []
    last_company = ""
    for cells in rows:
        if len(cells) <= max(cols.values()):
            continue
        raw_company = cells[cols["company"]]
        company = clean_cell(raw_company)
        if "↳" in raw_company or not company:
            company = last_company
        else:
            last_company = company
        title = clean_cell(cells[cols["role"]])
        url = extract_url(cells[cols["link"]])  # closed roles have no link
        location = clean_cell(cells[cols["location"]]) if "location" in cols else ""
        if company and title and url:
            jobs.append(make_job(source, company, title, location, url))
    return jobs


def parse_html_tables(source: str, text: str) -> list[Job]:
    """Parse every ``<table>`` in ``text`` whose headers look like a job table."""
    jobs: list[Job] = []
    for table in re.findall(r"(?is)<table[^>]*>(.*?)</table>", text):
        headers = [clean_cell(h) for h in re.findall(r"(?is)<th[^>]*>(.*?)</th>", table)]
        cols = column_map(headers)
        if not cols:
            continue
        rows = []
        for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table):
            cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
            if cells:
                rows.append(cells)
        jobs.extend(rows_to_jobs(source, rows, cols))
    return jobs


def parse_markdown_tables(source: str, text: str) -> list[Job]:
    """Parse every Markdown pipe table in ``text`` that looks like a job table."""
    jobs: list[Job] = []
    cols: dict[str, int] | None = None
    rows: list[list[str]] = []

    def flush() -> None:
        nonlocal rows
        if cols and rows:
            jobs.extend(rows_to_jobs(source, rows, cols))
        rows = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            flush()
            cols = None
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", stripped.strip("|"))]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) and any(cells):
            continue  # the |---|---| separator line
        maybe_header = column_map(cells)
        if maybe_header and cols is None:
            cols = maybe_header
            continue
        if cols:
            rows.append(cells)
    flush()
    return jobs


def fetch_github_feed(feed: dict[str, Any]) -> list[Job] | None:
    """Fetch and parse a GitHub-hosted README job tracker; None on failure."""
    url = feed.get("url", "")
    source = f"feed:{url}"
    try:
        resp = http_get(url)
        if resp.status_code == 404:
            log.warning("[feed] 404 for %s - the file moved or the URL is wrong.", url)
            return None
        resp.encoding = "utf-8"
        text = resp.text
    except requests.RequestException as e:
        log.warning("[feed] fetch failed for %s: %s", url, e)
        return None
    jobs = parse_html_tables(source, text) + parse_markdown_tables(source, text)
    if not jobs:
        log.warning("[feed] no table rows parsed from %s (format may have changed).", url)
        return None
    return jobs


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def _header_value(value: str) -> str:
    """HTTP headers must be ASCII; ntfy accepts RFC 2047 for anything else."""
    value = re.sub(r"[\r\n]+", " ", value)
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


class Notifier:
    """Sends alerts to ntfy and/or Discord; secrets are scrubbed from logs."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        n = cfg.get("notifications", {})
        server = str(n.get("ntfy_server", "https://ntfy.sh")).strip().rstrip("/")
        cfg_topic = str(n.get("ntfy_topic_name", "")).strip().strip("/")
        cfg_discord = str(n.get("discord_webhook_url", "")).strip()
        if cfg_topic == PLACEHOLDER_TOPIC:
            cfg_topic = ""
        # In a public repository everything in config.json is world-readable,
        # so the topic and webhook must come from GitHub Encrypted Secrets.
        if os.environ.get("REPO_VISIBILITY", "").strip().lower() == "public" and (cfg_topic or cfg_discord):
            sys.exit(
                "ERROR: this repository is PUBLIC but config.json contains your ntfy "
                "topic or Discord webhook, so anyone can read it.\n"
                "1. Put the placeholder back in config.json (ntfy_topic_name: "
                f"\"{PLACEHOLDER_TOPIC}\", discord_webhook_url: \"\").\n"
                "2. Pick a NEW topic name (the old one is in the git history) and "
                "subscribe to it in the ntfy app.\n"
                "3. Save it as a repository secret named NTFY_TOPIC "
                "(Settings > Secrets and variables > Actions)."
            )
        # The NTFY_TOPIC secret wins; config.json is the fallback for private/local use.
        topic = (os.environ.get("NTFY_TOPIC") or cfg_topic).strip().strip("/")
        self.ntfy_ready = bool(server and topic)
        # Endpoint = server + "/" + topic
        self.ntfy_url = f"{server}/{topic}" if self.ntfy_ready else ""
        self.discord_url = (os.environ.get("DISCORD_WEBHOOK_URL") or cfg_discord).strip()
        self._secrets = [x for x in (topic, self.discord_url) if x]
        self.max_alerts = max(1, int(n.get("max_individual_alerts_per_run", 15)))

    def _scrub(self, text: object) -> str:
        """Never let the topic or webhook reach the (public) Actions log."""
        out = str(text)
        for secret in self._secrets:
            out = out.replace(secret, "***")
        return out

    @property
    def configured(self) -> bool:
        """True if at least one delivery channel is set up."""
        return self.ntfy_ready or bool(self.discord_url)

    def send(self, title: str, message: str, url: str | None = None,
             priority: int = 3, tags: list[str] | None = None) -> bool:
        """Send to all configured channels; True if at least one accepted it."""
        ok = False
        if self.ntfy_ready:
            ok = self._ntfy(title, message, url, priority, tags or []) or ok
        if self.discord_url:
            ok = self._discord(title, message, url) or ok
        return ok

    def _ntfy(self, title: str, message: str, url: str | None, priority: int,
              tags: list[str]) -> bool:
        headers = {
            "Title": _header_value(title[:200]),
            "Priority": str(min(5, max(1, int(priority)))),
        }
        if tags:
            headers["Tags"] = ",".join(t for t in tags if t)
        if url:
            safe_url = requests.utils.requote_uri(url)
            headers["Click"] = safe_url
            if "," not in safe_url and ";" not in safe_url:
                headers["Actions"] = f"view, Open posting, {safe_url}"
        body = message.encode("utf-8")[:3900].decode("utf-8", "ignore").encode("utf-8")
        try:
            resp = SESSION.post(self.ntfy_url, data=body,
                                headers=headers, timeout=20)
            if resp.status_code == 429:
                time.sleep(10)
                resp = SESSION.post(self.ntfy_url, data=body,
                                    headers=headers, timeout=20)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("ntfy send failed: %s", self._scrub(e))
            return False

    def _discord(self, title: str, message: str, url: str | None) -> bool:
        content = f"**{title}**\n{message}"
        if url and url not in content:
            content += f"\n{url}"
        payload = {"content": content[:1900]}
        try:
            resp = SESSION.post(self.discord_url, json=payload, timeout=20)
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("retry_after", 2))
                except ValueError:
                    wait = 2
                time.sleep(min(wait, 15) + 0.5)
                resp = SESSION.post(self.discord_url, json=payload, timeout=20)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("Discord send failed: %s", self._scrub(e))
            return False


def badges(categories: list[Category]) -> str:
    """Render categories as ``[Label] [Label]``."""
    return " ".join(f"[{c['label']}]" for c in categories)


def send_job_alert(notifier: Notifier, job: Job, categories: list[Category]) -> bool:
    """Push one job alert; priority comes from the highest-priority category."""
    top = max(categories, key=lambda c: c["priority"])
    title = f"{badges(categories)} {job['company']}: {job['title']}"
    message = (
        f"Company: {job['company']}\n"
        f"Role: {job['title']}\n"
        f"Location: {job['location'] or 'Not listed'}\n"
        f"Category: {badges(categories)}\n"
        f"{job['url']}"
    )
    tags = [c["emoji_tag"] for c in categories if c["emoji_tag"]]
    return notifier.send(title, message, url=job["url"], priority=top["priority"],
                         tags=list(dict.fromkeys(tags)))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def collect_sources(cfg: dict[str, Any]) -> Iterator[tuple[str, Callable[[], list[Job] | None]]]:
    """Yield ``(source_key, fetch_function)`` for everything enabled in config."""
    companies = cfg.get("companies", {})
    for board, fetcher in (("greenhouse", fetch_greenhouse), ("lever", fetch_lever)):
        entries = companies.get(board, {})
        if isinstance(entries, list):  # also accept a plain list of slugs
            entries = {slug: slug for slug in entries}
        for slug, name in entries.items():
            slug = str(slug).strip()
            if slug:
                yield f"{board}:{slug}", (lambda f=fetcher, s=slug, n=name: f(s, str(n or s)))
    for feed in cfg.get("github_feeds", []):
        if isinstance(feed, str):
            feed = {"url": feed, "enabled": True}
        if feed.get("enabled", True) and feed.get("url"):
            yield f"feed:{feed['url']}", (lambda fd=feed: fetch_github_feed(fd))


def dedupe_candidates(candidates: list[tuple[Job, list[Category], str]],
                      state: State, today: str, enabled: bool = True
                      ) -> list[tuple[Job, list[Category], str, str]]:
    """Drop roles already alerted via another source (same company + title).

    Dropped jobs are marked seen in ``state``. Returns ``(job, cats, jid, key)``.
    """
    to_send: list[tuple[Job, list[Category], str, str]] = []
    sent_keys: set[str] = set()
    for job, cats, jid in candidates:
        key = soft_key(job)
        if enabled and (key in state["alerted_keys"] or key in sent_keys):
            state["seen"][jid] = today
            continue
        sent_keys.add(key)
        to_send.append((job, cats, jid, key))
    return to_send


def main() -> None:
    """Run one scrape-filter-notify cycle."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config()
    matcher = Matcher(cfg)
    notifier = Notifier(cfg)
    state, first_run = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dedupe_soft = bool(cfg.get("matching", {}).get("dedupe_across_sources", True))

    if not matcher.categories:
        log.warning("no role category is enabled in config.json; nothing can match.")

    dry_run = not notifier.configured
    if dry_run:
        log.warning("No ntfy topic found. Add a repository secret named NTFY_TOPIC "
                    "(Settings > Secrets and variables > Actions). Running in DRY-RUN "
                    "mode: matches are logged, nothing is sent and seen_jobs.json "
                    "is not written.")

    known_sources = set(state["sources"])
    baselined = 0           # existing roles recorded silently
    new_sources: list[str] = []   # sources seen for the first time (after day one)
    candidates: list[tuple[Job, list[Category], str]] = []  # to alert on
    ok_sources = failed_sources = total_jobs = total_matches = 0

    for source_key, fetch in collect_sources(cfg):
        jobs = fetch()
        if jobs is None:
            failed_sources += 1
            continue
        ok_sources += 1
        total_jobs += len(jobs)
        is_new_source = first_run or source_key not in known_sources
        matched_here = 0
        for job in jobs:
            cats = matcher.match(job)
            if not cats:
                continue
            matched_here += 1
            jid = job_id(job)
            if jid in state["seen"]:
                continue
            if is_new_source:
                # Day one, or a company/feed you just added: record, don't ping.
                state["seen"][jid] = today
                state["alerted_keys"].setdefault(soft_key(job), today)
                baselined += 1
                if dry_run:
                    log.info("MATCH %s %s | %s | %s | %s", badges(cats), job["company"],
                             job["title"], job["location"], job["url"])
            else:
                candidates.append((job, cats, jid))
        total_matches += matched_here
        if is_new_source:
            state["sources"].append(source_key)
            if not first_run:
                new_sources.append(source_key)
        log.info("[%s] %d jobs, %d match filters", source_key[:70], len(jobs), matched_here)

    log.info("Sources OK: %d, failed: %d, jobs scanned: %d, matching: %d",
             ok_sources, failed_sources, total_jobs, total_matches)

    if ok_sources == 0:
        sys.exit("ERROR: every source failed to load; not changing seen_jobs.json.")

    if dry_run:
        log.info("DRY-RUN complete: %d roles currently match your filters.", baselined)
        return

    if first_run:
        notifier.send(
            "Internship bot initialized",
            f"Bot initialized! Found {baselined} existing matching roles. "
            "Future alerts will ping individually.",
            priority=3, tags=["white_check_mark"])
        state["initialized"] = True
        save_state(state)
        log.info("First run: baselined %d roles, sent one summary notification.", baselined)
        return

    state["initialized"] = True

    if new_sources and baselined:
        names = ", ".join(s.split(":", 1)[1][:40] for s in new_sources[:5])
        notifier.send(
            "New source added",
            f"Now watching {len(new_sources)} new source(s): {names}. "
            f"{baselined} existing matching roles were recorded without pinging. "
            "New postings will alert individually.",
            priority=2, tags=["heavy_plus_sign"])

    to_send = dedupe_candidates(candidates, state, today, dedupe_soft)

    # Highest-priority categories first.
    to_send.sort(key=lambda item: -max(c["priority"] for c in item[1]))
    individual, overflow = to_send[:notifier.max_alerts], to_send[notifier.max_alerts:]

    sent = 0
    for job, cats, jid, key in individual:
        if send_job_alert(notifier, job, cats):
            state["seen"][jid] = today
            state["alerted_keys"][key] = today
            sent += 1
            log.info("ALERT %s %s | %s", badges(cats), job["company"], job["title"])
            time.sleep(0.6)
        # On failure the job stays unseen and is retried next run.

    if overflow:
        lines = [f"- {j['company']}: {j['title']} ({j['location']})\n  {j['url']}"
                 for j, _, _, _ in overflow]
        body = f"{len(overflow)} more new matching roles this run:\n" + "\n".join(lines)
        if notifier.send(f"{len(overflow)} more new internship matches", body,
                         priority=3, tags=["card_index_dividers"]):
            for job, cats, jid, key in overflow:
                state["seen"][jid] = today
                state["alerted_keys"][key] = today

    save_state(state)
    log.info("Done: %d individual alert(s), %d in digest.", sent, len(overflow))


if __name__ == "__main__":
    main()
