"""Unit tests for the filtering engine, using mock job payloads (no network)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper  # noqa: E402

CFG = {
    "level_keywords": ["intern", "internship", "co-op"],
    "exclude_keywords": ["senior", "phd only"],
    "allowed_locations": ["ottawa", "remote", ", ca"],
    "role_categories": {
        "hw": {"enabled": True, "label": "Hardware", "priority": 5,
               "keywords": ["fpga", "soc", "hardware engineer"]},
        "emb": {"enabled": True, "label": "Embedded", "priority": 4,
                "keywords": ["firmware", "embedded"]},
        "off": {"enabled": False, "label": "Off", "keywords": ["fpga"]},
    },
}


def job(title, location="Ottawa, ON", company="Acme", url="https://x.test/1", department=""):
    return scraper.make_job("test", company, title, location, url, department)


@pytest.fixture
def matcher():
    return scraper.Matcher(CFG)


def labels(m, j):
    return [c["label"] for c in m.match(j)]


# --- tiered keyword matching -------------------------------------------------

def test_single_category_match(matcher):
    assert labels(matcher, job("FPGA Intern")) == ["Hardware"]


def test_multiple_categories_match(matcher):
    assert labels(matcher, job("Embedded FPGA Intern")) == ["Hardware", "Embedded"]


def test_department_counts_as_signal(matcher):
    assert labels(matcher, job("Engineering Intern", department="Firmware")) == ["Embedded"]


def test_disabled_category_ignored(matcher):
    assert [c["key"] for c in matcher.categories] == ["hw", "emb"]


def test_level_keyword_required(matcher):
    assert matcher.match(job("FPGA Engineer")) == []


def test_level_without_category_is_no_match(matcher):
    assert matcher.match(job("Marketing Intern")) == []


def test_dash_normalization(matcher):
    assert labels(matcher, job("Firmware Co‑op")) == ["Embedded"]  # non-breaking hyphen


# --- whole-word boundaries ---------------------------------------------------

def test_intern_not_internal(matcher):
    assert matcher.match(job("Internal FPGA Engineer")) == []


def test_soc_not_social(matcher):
    assert matcher.match(job("Social Media Intern")) == []


def test_plural_and_ing_endings(matcher):
    assert labels(matcher, job("FPGAs Interns")) == ["Hardware"]
    assert labels(matcher, job("Hardware Engineering Intern")) == ["Hardware"]


def test_substring_mode_when_whole_words_off():
    cfg = {**CFG, "matching": {"match_whole_words": False}}
    assert scraper.Matcher(cfg).match(job("Internal FPGA Engineer")) != []


# --- exclude words -----------------------------------------------------------

def test_exclude_word_blocks(matcher):
    assert matcher.match(job("Senior FPGA Intern")) == []


def test_exclude_is_whole_word(matcher):
    # "seniority" must not trip the "senior" exclude
    assert labels(matcher, job("FPGA Intern seniority program")) == ["Hardware"]


# --- location ----------------------------------------------------------------

def test_location_allowed(matcher):
    assert matcher.match(job("FPGA Intern", location="Remote - US"))


def test_location_state_suffix(matcher):
    assert matcher.match(job("FPGA Intern", location="San Jose, CA"))


def test_location_rejected(matcher):
    assert matcher.match(job("FPGA Intern", location="Berlin, Germany")) == []


def test_unknown_location_rejected_by_default(matcher):
    assert matcher.match(job("FPGA Intern", location="")) == []


def test_unknown_location_allowed_by_option():
    cfg = {**CFG, "matching": {"allow_unknown_location": True}}
    assert scraper.Matcher(cfg).match(job("FPGA Intern", location=""))


# --- cross-source dedup ------------------------------------------------------

def test_soft_key_ignores_url_case_and_punctuation():
    a = job("FPGA Intern", url="https://a.test/1", company="Acme, Inc.")
    b = job("fpga  intern!", url="https://b.test/2", company="ACME Inc")
    assert scraper.soft_key(a) == scraper.soft_key(b)
    assert scraper.job_id(a) != scraper.job_id(b)


def test_dedupe_drops_already_alerted_role():
    a, b = job("FPGA Intern", url="https://a.test/1"), job("FPGA Intern", url="https://b.test/2")
    state = {"seen": {}, "alerted_keys": {scraper.soft_key(a): "2026-01-01"}}
    out = scraper.dedupe_candidates([(b, [], scraper.job_id(b))], state, "2026-02-02")
    assert out == []
    assert scraper.job_id(b) in state["seen"]  # marked seen so it is not retried


def test_dedupe_collapses_duplicates_within_run():
    a, b = job("FPGA Intern", url="https://a.test/1"), job("FPGA Intern", url="https://b.test/2")
    state = {"seen": {}, "alerted_keys": {}}
    cands = [(a, [], scraper.job_id(a)), (b, [], scraper.job_id(b))]
    assert len(scraper.dedupe_candidates(cands, state, "2026-02-02")) == 1


def test_dedupe_disabled_keeps_both():
    a, b = job("FPGA Intern", url="https://a.test/1"), job("FPGA Intern", url="https://b.test/2")
    state = {"seen": {}, "alerted_keys": {}}
    cands = [(a, [], scraper.job_id(a)), (b, [], scraper.job_id(b))]
    assert len(scraper.dedupe_candidates(cands, state, "2026-02-02", enabled=False)) == 2


# --- feed parsing ------------------------------------------------------------

def test_markdown_table_parsing():
    md = (
        "| Company | Role | Location | Application |\n"
        "|---|---|---|---|\n"
        "| **Acme** | FPGA Intern | Ottawa, ON | [Apply](https://acme.test/j1) |\n"
        "| ↳ | Firmware Intern | Remote | [Apply](https://acme.test/j2) |\n"
    )
    jobs = scraper.parse_markdown_tables("feed:x", md)
    assert [(j["company"], j["title"]) for j in jobs] == [("Acme", "FPGA Intern"),
                                                          ("Acme", "Firmware Intern")]


def test_topic_with_bom_and_newline_is_cleaned(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "\ufeffmy-topic-123\r\n")
    assert scraper.Notifier({}).ntfy_url == "https://ntfy.sh/my-topic-123"


def test_parse_posted_formats():
    from datetime import date
    t = date(2026, 11, 15)
    assert scraper.parse_posted("5d", t) == "2026-11-10"
    assert scraper.parse_posted("1mo", t) == "2026-10-16"
    assert scraper.parse_posted("Nov 14", t) == "2026-11-14"
    assert scraper.parse_posted("Dec 30", t) == "2025-12-30"
    assert scraper.parse_posted("2026-10-19T11:17:24-04:00", t) == "2026-10-19"
    assert scraper.parse_posted(1786469891368, t) == "2026-08-11"
    assert scraper.parse_posted("garbage", t) == ""


def test_feed_age_column_and_matches_page_order(tmp_path):
    md = (
        "| Company | Role | Location | Application | Age |\n|---|---|---|---|---|\n"
        "| A | FPGA Intern | Ottawa | [x](https://a.test/1) | 9d |\n"
        "| B | FPGA Intern | Ottawa | [x](https://b.test/1) | 1d |\n"
    )
    jobs = scraper.parse_markdown_tables("feed:x", md)
    assert jobs[0]["posted"] < jobs[1]["posted"]
    page = tmp_path / "m.md"
    scraper.write_matches_page(jobs, str(page))
    text = page.read_text(encoding="utf-8")
    assert text.index("**B**") < text.index("**A**")  # newest first

def test_ntfy_headers_include_matches_button(monkeypatch):
    sent = {}

    class R:
        status_code = 200
        def raise_for_status(self): pass

    monkeypatch.setattr(scraper.SESSION, "post",
                        lambda url, data, headers, timeout: sent.update(h=headers) or R())
    n = scraper.Notifier({"notifications": {"ntfy_topic_name": "t", "matches_url": "https://list.test/m"}})
    n.send("t", "m", url="https://job.test/1")
    assert sent["h"]["Click"] == "https://job.test/1"
    assert "All matches, https://list.test/m" in sent["h"]["Actions"]
    n.send("digest", "m")  # no job url: tap opens the list
    assert sent["h"]["Click"] == "https://list.test/m"

def test_matches_html_written_and_escaped(tmp_path):
    jobs = [scraper.make_job("s", "A<b>", "FPGA <script>x</script> Intern", "Ottawa",
                             'https://a.test/1?x="y"', posted="2026-11-14")]
    scraper.write_matches_page(jobs, str(tmp_path / "m.md"))
    page = (tmp_path / "m.html").read_text(encoding="utf-8")
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert "1 matching internships" in page