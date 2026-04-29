"""Tests for agents/connector/scripts/morning-relationship-nudge.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`connector:morning-relationship-nudge`. Runs people-scan.py, formats the
nudge, and writes to cache/morning-brief-ready.txt for the
5 AM PT fleet delivery path. On Mondays the weekly-review cron is folded
in (Option C) via an additional MONDAY recap section.

TDD: tests land before the implementation.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "connector" / "scripts" / "morning-relationship-nudge.py"
FIXTURES = Path(__file__).parent / "fixtures" / "morning-relationship-nudge"


def _load_module():
    spec = importlib.util.spec_from_file_location("morning_relationship_nudge", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mrn():
    return _load_module()


@pytest.fixture
def scan_mixed():
    with open(FIXTURES / "people-scan-mixed.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def scan_empty():
    with open(FIXTURES / "people-scan-empty.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def scan_truncated():
    with open(FIXTURES / "people-scan-truncated.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def tuesday_pacific():
    """2026-04-14 03:00 Pacific — Tuesday (weekday == 1, no Monday recap)."""
    return datetime(2026, 4, 14, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


@pytest.fixture
def monday_pacific():
    """2026-04-20 03:00 Pacific — Monday (weekday == 0, Monday recap appended)."""
    return datetime(2026, 4, 20, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


# ─── _render_contact_link: clickable mailto/tel over plain channel label ──


def test_render_contact_link_email_preferred(mrn):
    """When preferred_channel is 'email' and the entry has an email,
    render a <a href="mailto:..."> anchor containing the email address."""
    entry = {
        "name": "Alice Smith",
        "preferred_channel": "email",
        "email": "alice@example.com",
        "phone": "+14155550100",
    }
    out = mrn._render_contact_link(entry)
    assert out == '<a href="mailto:alice@example.com">alice@example.com</a>'


def test_render_contact_link_phone_channels_render_tel(mrn):
    """Channels that map to phone (text, Messages, SMS, WhatsApp,
    Signal, phone) must render a tel: anchor with the phone number."""
    for channel in ("text", "Messages", "SMS", "whatsapp", "Signal", "phone"):
        entry = {
            "name": "Bob",
            "preferred_channel": channel,
            "email": "bob@example.com",
            "phone": "+14155550100",
        }
        out = mrn._render_contact_link(entry)
        assert out == '<a href="tel:+14155550100">+14155550100</a>', (
            f"channel={channel!r} should render tel anchor"
        )


def test_render_contact_link_fills_from_available_when_preferred_missing(mrn):
    """If preferred_channel asks for a phone number but the entry only
    has an email, fall back to the email anchor (don't drop the link)."""
    entry = {
        "name": "Cara",
        "preferred_channel": "text",
        "email": "cara@example.com",
        "phone": "",
    }
    out = mrn._render_contact_link(entry)
    assert out == '<a href="mailto:cara@example.com">cara@example.com</a>'


def test_render_contact_link_falls_back_to_channel_label_when_no_contacts(mrn):
    """No email and no phone → don't try to build an anchor; fall back
    to the plain channel label so the message is never empty."""
    entry = {
        "name": "Dan",
        "preferred_channel": "text",
        "email": "",
        "phone": "",
    }
    out = mrn._render_contact_link(entry)
    assert out == "text"


def test_render_contact_link_empty_when_no_channel_and_no_contacts(mrn):
    entry = {"name": "Eve", "preferred_channel": "", "email": "", "phone": ""}
    assert mrn._render_contact_link(entry) == ""


def test_render_contact_link_escapes_html_metacharacters_in_fallback(mrn):
    """If for some reason a channel label contains an HTML-meta char
    (&, <, >), the fallback must be HTML-escaped so it doesn't break
    parse_mode=HTML delivery."""
    entry = {
        "name": "Finn",
        "preferred_channel": "text & chat",
        "email": "",
        "phone": "",
    }
    out = mrn._render_contact_link(entry)
    assert "&amp;" in out
    assert "<" not in out
    assert ">" not in out


def test_format_nudge_renders_mailto_when_email_present(
    mrn, tuesday_pacific
):
    """Integration: nudge body for a person with email + preferred_channel=email
    contains a mailto anchor, NOT a plain 'email' label."""
    scan = {
        "status": "ok",
        "overdue_total": 1,
        "overdue_by_group": {
            "family": [],
            "friends": [],
            "colleagues": [
                {
                    "slug": "thomas-example",
                    "name": "Thomas Example",
                    "preferred_channel": "email",
                    "email": "thomas@example.com",
                    "phone": "",
                    "days_since": 120,
                    "days_overdue": 30,
                    "display_group": "colleagues",
                }
            ],
        },
        "summary": {"total": 42},
    }
    body = mrn.format_nudge(scan, tuesday_pacific)
    assert '<a href="mailto:thomas@example.com">thomas@example.com</a>' in body


def test_build_nudge_items_person_text_uses_contact_link(
    mrn, tuesday_pacific
):
    """morning-fleet-deliver consumes cache/morning-items.json; the
    person item's `text` field must carry the anchor so the Telegram
    send renders it clickable."""
    scan = {
        "overdue_total": 1,
        "overdue_by_group": {
            "family": [],
            "friends": [
                {
                    "slug": "bob",
                    "name": "Bob",
                    "preferred_channel": "text",
                    "email": "",
                    "phone": "+14155551234",
                    "days_since": 60,
                    "display_group": "friends",
                }
            ],
            "colleagues": [],
        },
        "summary": {"total": 10},
    }
    items = mrn.build_nudge_items(scan, tuesday_pacific)
    person_items = [i for i in items if i.get("type") == "person"]
    assert len(person_items) == 1
    assert '<a href="tel:+14155551234">+14155551234</a>' in person_items[0]["text"]


# ─── format_nudge: happy paths ───────────────────────────────────────


def test_format_nudge_includes_header_with_pacific_date(mrn, scan_mixed, tuesday_pacific):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    assert "Relationship Check" in body
    assert "Tuesday, April 14" in body


def test_format_nudge_groups_overdue_by_circle(
    mrn, scan_mixed, tuesday_pacific
):
    """Overdue entries render under per-circle section headers
    (FAMILY / FRIENDS / COLLEAGUES), each with its own count."""
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    # Both groups with entries appear
    assert "FAMILY" in body
    assert "FRIENDS" in body
    # Alice (friends-close) under FRIENDS
    assert "Alice Smith" in body
    assert "58 days" in body
    assert "text" in body
    # Bob (family-extended) under FAMILY
    assert "Bob Jones" in body
    assert "35 days" in body
    assert "phone" in body


def test_format_nudge_omits_empty_groups(mrn, scan_mixed, tuesday_pacific):
    """COLLEAGUES has 0 entries in the mixed fixture — its header
    should not appear."""
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    assert "COLLEAGUES" not in body


def test_format_nudge_footer_counts(mrn, scan_mixed, tuesday_pacific):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    # Footer: X overdue · Z tracked (approaching dropped per 2026-04-16)
    assert "2 overdue" in body
    assert "42 tracked" in body
    assert "approaching" not in body.lower()


# ─── format_nudge: empty state ───────────────────────────────────────


def test_format_nudge_empty_uses_accounted_for_line(
    mrn, scan_empty, tuesday_pacific
):
    body = mrn.format_nudge(scan_empty, tuesday_pacific)
    assert "Everyone" in body and "accounted for" in body
    # No group headers when all groups empty
    assert "FAMILY" not in body
    assert "FRIENDS" not in body
    assert "COLLEAGUES" not in body


def test_format_nudge_empty_still_shows_tracked_count(
    mrn, scan_empty, tuesday_pacific
):
    body = mrn.format_nudge(scan_empty, tuesday_pacific)
    assert "42 tracked" in body


# ─── format_nudge: overdue_total > displayed overdue ─────────────────


def test_load_latest_triage_envelope_returns_most_recent(mrn, tmp_path):
    """The helper parses connector-inbox-triage-host.log and returns
    the envelope from the latest successful run. Earlier runs ignored."""
    log = tmp_path / "log.log"
    log.write_text(
        '[2026-04-29T01:00:01Z] connector-inbox-triage exit=0\n'
        '{"status": "ok", "queued": 1, "skipped_unknown_sender": 5, "skipped_service": 30}\n'
        '[2026-04-29T01:30:01Z] connector-inbox-triage exit=0\n'
        '{"status": "ok", "queued": 5, "skipped_unknown_sender": 2, "skipped_service": 35, '
        '"skipped_samples": [{"from_email": "alyssa@coinbase.com", "subject": "Meeting"}]}\n',
        encoding="utf-8",
    )
    env = mrn._load_latest_triage_envelope(log)
    assert env is not None
    assert env["queued"] == 5
    assert env["skipped_unknown_sender"] == 2
    assert len(env["skipped_samples"]) == 1


def test_load_latest_triage_envelope_skips_error_runs(mrn, tmp_path):
    """If the most recent run errored, fall back to the prior ok envelope."""
    log = tmp_path / "log.log"
    log.write_text(
        '[2026-04-29T01:00:01Z] connector-inbox-triage exit=0\n'
        '{"status": "ok", "queued": 5, "skipped_unknown_sender": 2}\n'
        '[2026-04-29T01:30:01Z] connector-inbox-triage exit=0\n'
        '{"status": "error", "error": "OAuth refresh failed"}\n',
        encoding="utf-8",
    )
    env = mrn._load_latest_triage_envelope(log)
    assert env is not None
    assert env["queued"] == 5


def test_load_latest_triage_envelope_no_log(mrn, tmp_path):
    env = mrn._load_latest_triage_envelope(tmp_path / "missing.log")
    assert env is None


def test_format_triage_health_line_minimal(mrn):
    env = {"queued": 5, "skipped_unknown_sender": 0, "skipped_service": 32}
    line = mrn._format_triage_health_lines(env)
    assert any("5" in s and "drafted" in s for s in line)
    # No unknown samples — no expansion lines.
    assert all("•" not in s for s in line)


def test_format_triage_health_line_lists_unknowns(mrn):
    env = {
        "queued": 3,
        "skipped_unknown_sender": 2,
        "skipped_service": 30,
        "skipped_samples": [
            {"from_email": "alyssa@coinbase.com", "subject": "Meeting follow-up"},
            {"from_email": "ergaut@usfca.edu", "subject": "Tech Econ Seminar"},
        ],
    }
    lines = mrn._format_triage_health_lines(env)
    text = "\n".join(lines)
    assert "alyssa@coinbase.com" in text
    assert "ergaut@usfca.edu" in text
    assert "Meeting follow-up" in text


def test_format_nudge_appends_triage_health_when_provided(
    mrn, scan_mixed, tuesday_pacific,
):
    env = {
        "queued": 4,
        "skipped_unknown_sender": 1,
        "skipped_service": 30,
        "skipped_samples": [
            {"from_email": "alyssa@coinbase.com", "subject": "Meeting"},
        ],
    }
    body = mrn.format_nudge(scan_mixed, tuesday_pacific, triage_health=env)
    assert "Triage" in body  # one of the rendered lines
    assert "alyssa@coinbase.com" in body


def test_format_nudge_omits_triage_when_health_is_none(
    mrn, scan_mixed, tuesday_pacific,
):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific, triage_health=None)
    assert "Triage" not in body


def test_format_nudge_truncated_footer_shows_full_total(
    mrn, scan_truncated, tuesday_pacific
):
    """When per-group cap truncates display (8 overdue, top-5 shown),
    the footer overdue_total preserves visibility of the full count."""
    body = mrn.format_nudge(scan_truncated, tuesday_pacific)
    # Footer shows full 8 overdue even though only 5 rendered
    assert "8 overdue" in body


# ─── format_nudge: Monday fold ───────────────────────────────────────


def test_format_nudge_monday_adds_recap_section(mrn, scan_mixed, monday_pacific):
    body = mrn.format_nudge(scan_mixed, monday_pacific)
    # Monday section flag visible
    assert "MONDAY" in body
    # Total tracked called out
    assert "42" in body


def test_format_nudge_non_monday_has_no_recap_section(
    mrn, scan_mixed, tuesday_pacific
):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    assert "MONDAY" not in body


def test_format_nudge_monday_empty_still_has_recap(
    mrn, scan_empty, monday_pacific
):
    """Monday recap should appear even when the daily state is
    everyone-accounted-for — the recap is the point of the Monday fold."""
    body = mrn.format_nudge(scan_empty, monday_pacific)
    assert "MONDAY" in body
    assert "42" in body


# ─── run() orchestration ─────────────────────────────────────────────


def test_run_writes_brief_to_cache_file(mrn, scan_mixed, tmp_path, monkeypatch):
    workspace = tmp_path / "connector-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mrn, "WORKSPACE", workspace)
    monkeypatch.setattr(mrn, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mrn, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mrn, "BRIEF_FILE", workspace / "cache" / "morning-brief-ready.txt"
    )
    monkeypatch.setattr(
        mrn, "LAST_RUN_FILE", workspace / "cache" / "last-morning-nudge.json"
    )

    calls: list[tuple] = []

    def fake_run_script(script_name, *args, timeout=120):
        calls.append((script_name, args))
        if script_name == "people-scan.py":
            return scan_mixed
        return None

    monkeypatch.setattr(mrn, "_run_script", fake_run_script)

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            # Tuesday 2026-04-14 10:30 UTC = 3:30 AM Pacific
            return datetime(2026, 4, 14, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mrn, "datetime", _FrozenDt)

    result = mrn.run()

    assert result["status"] == "ok"
    brief = workspace / "cache" / "morning-brief-ready.txt"
    assert brief.exists()
    body = brief.read_text(encoding="utf-8")
    assert "Relationship Check" in body
    assert "Alice Smith" in body
    # Called people-scan exactly once
    assert [c[0] for c in calls] == ["people-scan.py"]


def test_run_handles_missing_people_scan_output(mrn, tmp_path, monkeypatch):
    """If people-scan.py fails (returns None), run() should still write
    a brief with an error-aware fallback and return degraded, not crash."""
    workspace = tmp_path / "connector-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mrn, "WORKSPACE", workspace)
    monkeypatch.setattr(mrn, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mrn, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mrn, "BRIEF_FILE", workspace / "cache" / "morning-brief-ready.txt"
    )
    monkeypatch.setattr(
        mrn, "LAST_RUN_FILE", workspace / "cache" / "last-morning-nudge.json"
    )

    monkeypatch.setattr(mrn, "_run_script", lambda *a, **k: None)

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 14, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mrn, "datetime", _FrozenDt)

    result = mrn.run()
    assert result["status"] == "degraded"
    assert "alert" in result


def test_run_sets_monday_flag_in_summary(mrn, scan_empty, tmp_path, monkeypatch):
    workspace = tmp_path / "connector-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mrn, "WORKSPACE", workspace)
    monkeypatch.setattr(mrn, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mrn, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mrn, "BRIEF_FILE", workspace / "cache" / "morning-brief-ready.txt"
    )
    monkeypatch.setattr(
        mrn, "LAST_RUN_FILE", workspace / "cache" / "last-morning-nudge.json"
    )
    monkeypatch.setattr(
        mrn, "_run_script", lambda *a, **k: scan_empty
    )

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            # Monday 2026-04-20 10:30 UTC = 3:30 AM Pacific
            return datetime(2026, 4, 20, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mrn, "datetime", _FrozenDt)

    result = mrn.run()
    assert result["status"] == "ok"
    assert result.get("monday_recap_included") is True
    body = (workspace / "cache" / "morning-brief-ready.txt").read_text(encoding="utf-8")
    assert "MONDAY" in body


# ─── main() contract ─────────────────────────────────────────────────


def test_main_always_exits_zero_on_error(mrn, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(mrn, "run", boom)

    rc = mrn.main()
    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out.strip().split("\n")[-1])
    assert payload["status"] == "error"
    assert "simulated failure" in payload["error"]
