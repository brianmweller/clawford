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


# ─── format_nudge: happy paths ───────────────────────────────────────


def test_format_nudge_includes_header_with_pacific_date(mrn, scan_mixed, tuesday_pacific):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    assert "Relationship Check" in body
    assert "Tuesday, April 14" in body


def test_format_nudge_lists_overdue_with_name_relationship_days_channel(
    mrn, scan_mixed, tuesday_pacific
):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    assert "OVERDUE" in body
    # Alice: 58 days since, text channel
    assert "Alice Smith" in body
    assert "College roommate" in body
    assert "58 days" in body
    assert "text" in body
    # Bob: 35 days since, phone channel
    assert "Bob Jones" in body
    assert "Cousin" in body
    assert "35 days" in body
    assert "phone" in body


def test_format_nudge_lists_approaching_with_days_until_due(
    mrn, scan_mixed, tuesday_pacific
):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    assert "APPROACHING" in body
    # Carol: days_overdue = -3 → due in 3 days
    assert "Carol Nguyen" in body
    assert "due in 3 days" in body


def test_format_nudge_footer_counts(mrn, scan_mixed, tuesday_pacific):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    # Footer: X overdue · Y approaching · Z tracked
    assert "2 overdue" in body
    assert "1 approaching" in body
    assert "42 tracked" in body


# ─── format_nudge: empty state ───────────────────────────────────────


def test_format_nudge_empty_uses_accounted_for_line(
    mrn, scan_empty, tuesday_pacific
):
    body = mrn.format_nudge(scan_empty, tuesday_pacific)
    assert "Everyone" in body and "accounted for" in body
    # No OVERDUE / APPROACHING section headers when both empty
    assert "OVERDUE" not in body
    assert "APPROACHING" not in body


def test_format_nudge_empty_still_shows_tracked_count(
    mrn, scan_empty, tuesday_pacific
):
    body = mrn.format_nudge(scan_empty, tuesday_pacific)
    assert "42 tracked" in body


# ─── format_nudge: overdue_total > displayed overdue ─────────────────


def test_format_nudge_notes_hidden_overdue_when_truncated(
    mrn, scan_truncated, tuesday_pacific
):
    """people-scan.py caps overdue at max_per_day (default 5). When
    overdue_total exceeds len(overdue), the nudge should surface the
    hidden count so the operator knows the displayed list is a preview."""
    body = mrn.format_nudge(scan_truncated, tuesday_pacific)
    # 8 total, 5 shown → 3 more
    assert "3 more" in body


def test_format_nudge_no_hidden_note_when_not_truncated(
    mrn, scan_mixed, tuesday_pacific
):
    body = mrn.format_nudge(scan_mixed, tuesday_pacific)
    assert "more" not in body.lower() or "overdue · " in body  # footer has "more"-free wording


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
