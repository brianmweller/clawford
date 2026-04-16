"""Tests for the upcoming-meeting filter in people-scan.py.

people-scan.py classifies people as overdue / approaching / healthy
based on days_since_last_interaction vs circle cadence. This file
tests the Component C addition: anyone with a confirmed upcoming
calendar meeting in the next 14 days should NOT show up in
overdue or approaching — they get demoted to a separate
`demoted_upcoming` bucket so Huckle Cat doesn't nag you to reach
out to someone you're literally meeting on Friday.

The upcoming set is loaded from
~/.clawford/connector-workspace/upcoming-meetings.json, which is
written by daily-refresh.py. When the file is missing, nothing is
demoted — backwards compatible with the pre-C behavior.

Run: cd agents/connector && python3 -m pytest tests/test_people_scan_upcoming.py -v
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"conn_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write_person(people_dir: Path, slug: str, *, email: str, last_interaction: str, circles: str = "friends-close"):
    (people_dir / f"{slug}.md").write_text(
        f"# {slug.replace('-', ' ').title()}\n"
        f"- **slug:** {slug}\n"
        f"- **circles:** {circles}\n"
        f"- **relationship:** friend\n"
        f"- **relationship_type:** friend\n"
        f"- **preferred_channel:** iMessage\n"
        f"- **tone:** warm\n"
        f"- **email:** {email}\n"
        f"- **phone:** —\n"
        f"- **platforms:** email\n"
        f"- **last_interaction:** {last_interaction}\n"
        f"- **context_notes:** test person\n"
        f"- **notes:** seeded for test\n",
        encoding="utf-8",
    )


def _write_config(workspace: Path):
    (workspace / "connector-config.json").write_text(json.dumps({
        "cadences": {
            "friends-close": {"check_days": 30, "nudge": True},
            "professional-inner": {"check_days": 7, "nudge": True},
            "family-inner": {"check_days": 1, "nudge": False},
        },
        "nudge": {"max_per_day": 5, "skip_circles": ["family-inner"]},
    }))


@pytest.fixture
def stub_brain(tmp_path, monkeypatch):
    people = tmp_path / "people"
    people.mkdir()
    workspace = tmp_path / "connector-workspace"
    workspace.mkdir()
    _write_config(workspace)

    ps = _load_script("people-scan.py")
    monkeypatch.setattr(ps, "BRAIN_PEOPLE", str(people))
    monkeypatch.setattr(ps, "CONFIG_FILE", str(workspace / "connector-config.json"))
    monkeypatch.setattr(ps, "UPCOMING_CACHE", str(workspace / "upcoming-meetings.json"))

    return type("Stub", (), {
        "ps": ps,
        "people": people,
        "workspace": workspace,
    })()


# ── _load_upcoming_meeting_emails ──────────────────────────────────


def test_load_upcoming_returns_empty_set_when_file_missing(stub_brain):
    # Upcoming cache file does not exist
    emails = stub_brain.ps._load_upcoming_meeting_emails()
    assert emails == set()


def test_load_upcoming_reads_emails_from_cache(stub_brain):
    (stub_brain.workspace / "upcoming-meetings.json").write_text(json.dumps({
        "generated_at": "2026-04-14T10:00:00+00:00",
        "window_days": 14,
        "emails": {
            "mohit@y.com": "2026-04-17",
            "alice@x.com": "2026-04-20",
        },
    }))
    emails = stub_brain.ps._load_upcoming_meeting_emails()
    assert emails == {"mohit@y.com", "alice@x.com"}


def test_load_upcoming_tolerates_malformed_json(stub_brain):
    (stub_brain.workspace / "upcoming-meetings.json").write_text("not json {")
    assert stub_brain.ps._load_upcoming_meeting_emails() == set()


# ── run() integration with the demotion filter ────────────────────


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()


def test_run_demotes_approaching_person_with_upcoming_meeting(stub_brain):
    # Mohit: last_interaction 25 days ago, friends-close (30d cadence)
    # → would be "approaching" without the filter
    _write_person(stub_brain.people, "mohit-kothari",
                  email="mohit@y.com",
                  last_interaction=_days_ago_iso(25))

    (stub_brain.workspace / "upcoming-meetings.json").write_text(json.dumps({
        "emails": {"mohit@y.com": "2026-04-17"},
    }))

    result = stub_brain.ps.run()
    assert result["status"] == "ok"
    assert not any(p["slug"] == "mohit-kothari" for p in result["approaching"])
    assert any(p["slug"] == "mohit-kothari" for p in result["demoted_upcoming"])
    assert result["summary"]["demoted_upcoming"] == 1


def test_run_demotes_overdue_person_with_upcoming_meeting(stub_brain):
    # 90 days ago, friends-close (30d) → 60 days overdue without filter
    _write_person(stub_brain.people, "alice-hyun",
                  email="alice@x.com",
                  last_interaction=_days_ago_iso(90))

    (stub_brain.workspace / "upcoming-meetings.json").write_text(json.dumps({
        "emails": {"alice@x.com": "2026-04-20"},
    }))

    result = stub_brain.ps.run()
    assert not any(p["slug"] == "alice-hyun" for p in result["overdue"])
    assert any(p["slug"] == "alice-hyun" for p in result["demoted_upcoming"])
    # Overdue count reflects the demotion
    assert result["summary"]["overdue"] == 0


def test_run_keeps_overdue_people_without_upcoming_meeting(stub_brain):
    _write_person(stub_brain.people, "charlie-farrell",
                  email="charlie@x.com",
                  last_interaction=_days_ago_iso(90))

    # No upcoming cache → filter is a no-op
    result = stub_brain.ps.run()
    assert any(p["slug"] == "charlie-farrell" for p in result["overdue"])
    assert result["demoted_upcoming"] == []
    assert result["summary"]["demoted_upcoming"] == 0


def test_run_case_insensitive_email_match(stub_brain):
    _write_person(stub_brain.people, "bob",
                  email="Bob@EXAMPLE.com",
                  last_interaction=_days_ago_iso(45))

    (stub_brain.workspace / "upcoming-meetings.json").write_text(json.dumps({
        "emails": {"bob@example.com": "2026-04-20"},
    }))

    result = stub_brain.ps.run()
    assert not any(p["slug"] == "bob" for p in result["overdue"])
    assert any(p["slug"] == "bob" for p in result["demoted_upcoming"])


def test_run_alt_emails_match_upcoming(stub_brain):
    """A meeting scheduled under a secondary email (alt_emails) still
    demotes the person."""
    (stub_brain.people / "andrew-patton.md").write_text(
        "# Andrew Patton\n"
        "- **slug:** andrew-patton\n"
        "- **circles:** friends-close\n"
        "- **preferred_channel:** iMessage\n"
        "- **tone:** warm\n"
        "- **email:** andrew.patton@duke.edu\n"
        "- **alt_emails:** pattonandrewj@gmail.com\n"
        "- **phone:** —\n"
        "- **platforms:** email\n"
        f"- **last_interaction:** {_days_ago_iso(45)}\n",
        encoding="utf-8",
    )

    (stub_brain.workspace / "upcoming-meetings.json").write_text(json.dumps({
        "emails": {"pattonandrewj@gmail.com": "2026-04-20"},
    }))

    result = stub_brain.ps.run()
    assert any(p["slug"] == "andrew-patton" for p in result["demoted_upcoming"])
    assert not any(p["slug"] == "andrew-patton" for p in result["overdue"])


def test_run_upcoming_filter_does_not_affect_healthy_people(stub_brain):
    """Someone fresh (well inside cadence) with an upcoming meeting
    should stay in healthy, not move to demoted_upcoming."""
    _write_person(stub_brain.people, "fresh-friend",
                  email="fresh@x.com",
                  last_interaction=_days_ago_iso(5))

    (stub_brain.workspace / "upcoming-meetings.json").write_text(json.dumps({
        "emails": {"fresh@x.com": "2026-04-20"},
    }))

    result = stub_brain.ps.run()
    # Fresh person is in healthy, NOT demoted
    assert any(p["slug"] == "fresh-friend" for p in result["healthy"])
    assert not any(p["slug"] == "fresh-friend" for p in result["demoted_upcoming"])


# ── person-ness filter (Am147 et al.) ─────────────────────────────


def test_run_skips_entries_with_no_email_and_no_phone(stub_brain):
    """Entries auto-created by the mining pipeline with no phone and
    no email (em-dash in both fields → parsed to None) are chat IDs
    or group placeholders, not people. They should NOT appear in
    overdue/approaching/healthy regardless of how stale the
    last_interaction is.

    Regression: 'Am147 (family) — 64 days since last contact' showed
    up in the OVERDUE list on 2026-04-16 even though Am147 is a
    WhatsApp chat ID, not a person."""
    (stub_brain.people / "am147.md").write_text(
        "# Am147\n"
        "- **slug:** am147\n"
        "- **circles:** friends-close\n"
        "- **preferred_channel:** WhatsApp\n"
        "- **tone:** casual\n"
        "- **email:** —\n"
        "- **phone:** —\n"
        "- **platforms:** whatsapp\n"
        f"- **last_interaction:** {_days_ago_iso(64)}\n"
        "- **notes:** Auto-created by mining pipeline on 2026-04-12.\n",
        encoding="utf-8",
    )
    # A real person alongside to prove the filter is narrow.
    _write_person(stub_brain.people, "real-friend",
                  email="friend@x.com",
                  last_interaction=_days_ago_iso(64))

    result = stub_brain.ps.run()
    assert not any(p["slug"] == "am147" for p in result["overdue"])
    assert not any(p["slug"] == "am147" for p in result["approaching"])
    assert not any(p["slug"] == "am147" for p in result["healthy"])
    # Real person still classified normally.
    assert any(p["slug"] == "real-friend" for p in result["overdue"])
    # Skipped counter increments.
    assert result["summary"]["skipped"] >= 1


def test_run_keeps_phone_only_person(stub_brain):
    """Person with a phone but no email (e.g. iMessage-only contacts)
    MUST still be scanned. Only entries with BOTH missing are
    filtered."""
    (stub_brain.people / "phone-only.md").write_text(
        "# Phone Only\n"
        "- **slug:** phone-only\n"
        "- **circles:** friends-close\n"
        "- **preferred_channel:** iMessage\n"
        "- **tone:** warm\n"
        "- **email:** —\n"
        "- **phone:** +1-555-0100\n"
        "- **platforms:** sms\n"
        f"- **last_interaction:** {_days_ago_iso(64)}\n",
        encoding="utf-8",
    )
    result = stub_brain.ps.run()
    assert any(p["slug"] == "phone-only" for p in result["overdue"])
