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
            "friends-acquaintance": {"check_days": 90, "nudge": True},
            "professional-inner": {"check_days": 7, "nudge": True},
            "professional-outer": {"check_days": 90, "nudge": True},
            "family-extended": {"check_days": 21, "nudge": True},
            "family-inner": {"check_days": 1, "nudge": False},
            "holiday-card": {"check_days": 365, "nudge": False},
        },
        "nudge": {
            "max_per_day": 5,
            "max_per_group": 5,
            "skip_circles": ["family-inner", "holiday-card"],
        },
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
    monkeypatch.setattr(ps, "SNOOZES_FILE", str(workspace / "snoozes.json"))

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


# ── circle grouping (family / friends / colleagues) ───────────────


def test_run_emits_overdue_by_group(stub_brain):
    """Each overdue person gets a display_group (family|friends|
    colleagues) and the run output includes `overdue_by_group` dict
    keyed by group with the top 5 per group."""
    _write_person(stub_brain.people, "aunt-marta", email="m@x.com",
                  last_interaction=_days_ago_iso(40),
                  circles="family-extended")
    _write_person(stub_brain.people, "best-friend", email="b@x.com",
                  last_interaction=_days_ago_iso(50),
                  circles="friends-close")
    _write_person(stub_brain.people, "work-lead", email="w@x.com",
                  last_interaction=_days_ago_iso(20),
                  circles="professional-inner")

    result = stub_brain.ps.run()
    by_group = result.get("overdue_by_group") or {}
    assert set(by_group.keys()) == {"family", "friends", "colleagues"}
    family_slugs = {p["slug"] for p in by_group["family"]}
    friends_slugs = {p["slug"] for p in by_group["friends"]}
    col_slugs = {p["slug"] for p in by_group["colleagues"]}
    assert "aunt-marta" in family_slugs
    assert "best-friend" in friends_slugs
    assert "work-lead" in col_slugs


def test_run_caps_each_group_at_top_5(stub_brain):
    """With 8 overdue friends, only the top 5 (most-overdue-first) land
    in overdue_by_group['friends']."""
    for i in range(8):
        _write_person(stub_brain.people, f"friend-{i:02d}",
                      email=f"f{i}@x.com",
                      last_interaction=_days_ago_iso(60 + i),
                      circles="friends-close")
    result = stub_brain.ps.run()
    by_group = result["overdue_by_group"]
    assert len(by_group["friends"]) == 5
    # Most-overdue first, so friend-07 (67 days) through friend-03
    # (63 days) are the 5 shown.
    shown = [p["slug"] for p in by_group["friends"]]
    assert shown[0] == "friend-07"
    assert shown[-1] == "friend-03"


def test_run_group_entries_carry_display_group_field(stub_brain):
    _write_person(stub_brain.people, "aunt-marta", email="m@x.com",
                  last_interaction=_days_ago_iso(40),
                  circles="family-extended")
    result = stub_brain.ps.run()
    entry = next(p for p in result["overdue"] if p["slug"] == "aunt-marta")
    assert entry.get("display_group") == "family"


# ── snooze state filter (Phase B) ─────────────────────────────────


def _write_snoozes(workspace: Path, snoozes: dict):
    (workspace / "snoozes.json").write_text(json.dumps(snoozes), encoding="utf-8")


def test_run_skips_person_with_active_snooze(stub_brain):
    """A person in snoozes.json with until-date in the future MUST
    NOT appear in overdue/approaching/healthy — they're deferred."""
    _write_person(stub_brain.people, "snoozed-friend", email="s@x.com",
                  last_interaction=_days_ago_iso(50),
                  circles="friends-close")
    # 30-day snooze starting today
    future = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    _write_snoozes(stub_brain.workspace, {
        "snoozed-friend": {
            "status": "snoozed",
            "until": future,
            "set_at": datetime.now(timezone.utc).isoformat(),
        },
    })
    result = stub_brain.ps.run()
    assert not any(p["slug"] == "snoozed-friend" for p in result["overdue"])
    assert not any(p["slug"] == "snoozed-friend" for p in result["approaching"])
    assert not any(p["slug"] == "snoozed-friend" for p in result["healthy"])


def test_run_includes_person_with_expired_snooze(stub_brain):
    """Once the snooze until-date has passed, the person resurfaces
    in overdue (so the operator isn't perma-hidden)."""
    _write_person(stub_brain.people, "expired-snooze-friend", email="e@x.com",
                  last_interaction=_days_ago_iso(50),
                  circles="friends-close")
    past = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    _write_snoozes(stub_brain.workspace, {
        "expired-snooze-friend": {
            "status": "snoozed",
            "until": past,
            "set_at": datetime.now(timezone.utc).isoformat(),
        },
    })
    result = stub_brain.ps.run()
    assert any(p["slug"] == "expired-snooze-friend" for p in result["overdue"])


def test_run_snoozes_file_missing_is_fine(stub_brain):
    """Backwards compat: if snoozes.json doesn't exist, behavior
    matches pre-Phase-B (no filtering)."""
    _write_person(stub_brain.people, "normal-friend", email="n@x.com",
                  last_interaction=_days_ago_iso(50),
                  circles="friends-close")
    result = stub_brain.ps.run()
    assert any(p["slug"] == "normal-friend" for p in result["overdue"])


def test_run_tolerates_malformed_snoozes_file(stub_brain):
    """A corrupt snoozes.json should NOT crash the scan — fall back
    to no-filter behavior and continue."""
    _write_person(stub_brain.people, "normal-friend", email="n@x.com",
                  last_interaction=_days_ago_iso(50),
                  circles="friends-close")
    (stub_brain.workspace / "snoozes.json").write_text("not json {",
                                                        encoding="utf-8")
    result = stub_brain.ps.run()
    assert result["status"] == "ok"
    assert any(p["slug"] == "normal-friend" for p in result["overdue"])


# ── auto-snooze on silence (Phase D) ──────────────────────────────


def test_run_auto_snoozes_slugs_shown_yesterday_without_action(stub_brain):
    """Any slug listed in last-shown-<yesterday>.json that has NO
    entry in snoozes.json gets auto-snoozed 3 days forward. This
    prevents the same list resurfacing every morning when the operator
    doesn't press any button, while keeping the hide-window shorter
    than the professional-inner cadence (7d) so the contact can
    re-surface later the same week."""
    _write_person(stub_brain.people, "unactioned-friend", email="u@x.com",
                  last_interaction=_days_ago_iso(50),
                  circles="friends-close")
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    (stub_brain.workspace / f"last-shown-{yesterday}.json").write_text(
        json.dumps({"slugs": ["unactioned-friend"]}),
        encoding="utf-8",
    )
    # snoozes.json does NOT yet contain unactioned-friend.

    result = stub_brain.ps.run()

    # unactioned-friend is now auto-snoozed and absent from overdue.
    assert not any(p["slug"] == "unactioned-friend" for p in result["overdue"])
    # snoozes.json now contains an entry for unactioned-friend.
    with open(stub_brain.workspace / "snoozes.json", encoding="utf-8") as f:
        snoozes = json.load(f)
    assert "unactioned-friend" in snoozes
    assert snoozes["unactioned-friend"]["status"] == "auto_snoozed"
    # Until-date is 3 days in the future (shortened from 14 on 2026-04-19).
    expected = (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()
    assert snoozes["unactioned-friend"]["until"] == expected


def test_run_does_not_auto_snooze_slugs_brian_already_actioned(stub_brain):
    """If the operator pressed done/snooze/ignore yesterday, his action
    must NOT be overwritten by the auto-snooze pass."""
    _write_person(stub_brain.people, "actioned-friend", email="a@x.com",
                  last_interaction=_days_ago_iso(50),
                  circles="friends-close")
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    (stub_brain.workspace / f"last-shown-{yesterday}.json").write_text(
        json.dumps({"slugs": ["actioned-friend"]}),
        encoding="utf-8",
    )
    # the operator already pressed "ignore" 365 days out yesterday.
    existing_until = (
        datetime.now(timezone.utc).date() + timedelta(days=364)
    ).isoformat()
    _write_snoozes(stub_brain.workspace, {
        "actioned-friend": {
            "status": "ignored",
            "until": existing_until,
            "set_at": datetime.now(timezone.utc).isoformat(),
        },
    })

    stub_brain.ps.run()

    with open(stub_brain.workspace / "snoozes.json", encoding="utf-8") as f:
        snoozes = json.load(f)
    # Status preserved (not overwritten to auto_snoozed).
    assert snoozes["actioned-friend"]["status"] == "ignored"
    assert snoozes["actioned-friend"]["until"] == existing_until


def test_run_no_last_shown_file_is_fine(stub_brain):
    """If no last-shown-<yesterday>.json exists (first day running or
    cron skipped), behavior falls back to no-auto-snooze."""
    _write_person(stub_brain.people, "friend", email="f@x.com",
                  last_interaction=_days_ago_iso(50),
                  circles="friends-close")
    result = stub_brain.ps.run()
    assert result["status"] == "ok"
    # No crash; no snoozes.json was created because no slugs to snooze.
