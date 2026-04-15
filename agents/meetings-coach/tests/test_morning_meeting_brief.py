"""Tests for agents/meetings-coach/scripts/morning-meeting-brief.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`meetings-coach:morning-meeting-brief`. Runs gcal-fetch --days 2,
meeting-prep per real meeting for open commitments, and writes
cache/morning-brief-ready.txt for the 5 AM PT fleet delivery path.
On Mondays it appends a WEEK AHEAD section built from --days 7 (Option
C fold — replaces the retired `weekly-review` cron).

Pure Python templating — calendar events + prep output are structured
(logic-gate rule).
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "meetings-coach" / "scripts" / "morning-meeting-brief.py"
FIXTURES = Path(__file__).parent / "fixtures" / "morning-meeting-brief"


def _load():
    spec = importlib.util.spec_from_file_location("morning_meeting_brief", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def gcal_today():
    with open(FIXTURES / "gcal-fetch-today.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def gcal_week():
    with open(FIXTURES / "gcal-fetch-week.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def prep_alexis():
    with open(FIXTURES / "meeting-prep-alexis.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def prep_board():
    with open(FIXTURES / "meeting-prep-board.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def tuesday_pacific():
    """Tuesday 2026-04-14 03:00 Pacific."""
    return datetime(2026, 4, 14, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


@pytest.fixture
def monday_pacific():
    """Monday 2026-04-20 03:00 Pacific."""
    return datetime(2026, 4, 20, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


# ─── split + filter helpers ──────────────────────────────────────────


def test_split_today_tomorrow_real_meetings_only(mod, gcal_today, tuesday_pacific):
    today, tomorrow = mod.split_today_tomorrow(gcal_today["events"], tuesday_pacific)
    ids_today = [e["id"] for e in today]
    ids_tomorrow = [e["id"] for e in tomorrow]
    assert "evt-today-1" in ids_today
    assert "evt-today-2" in ids_today
    # task block is NOT a real meeting — dropped
    assert "evt-today-task" not in ids_today
    assert "evt-tomorrow-1" in ids_tomorrow


def test_split_today_tomorrow_sorted_by_start(mod, gcal_today, tuesday_pacific):
    today, _ = mod.split_today_tomorrow(gcal_today["events"], tuesday_pacific)
    starts = [e["start"] for e in today]
    assert starts == sorted(starts)


# ─── format_brief ────────────────────────────────────────────────────


def test_format_brief_header_and_pacific_date(mod, gcal_today, tuesday_pacific):
    prep_lookup = {}
    body = mod.format_brief(gcal_today["events"], None, prep_lookup, tuesday_pacific)
    assert "Meeting Brief" in body
    assert "Tuesday, April 14" in body


def test_format_brief_lists_today_meetings_with_time(
    mod, gcal_today, tuesday_pacific
):
    body = mod.format_brief(gcal_today["events"], None, {}, tuesday_pacific)
    assert "Alexis 1:1" in body
    assert "Board prep review" in body
    assert "9:00" in body  # Alexis at 9:00 AM PT
    assert "2:00" in body  # Board at 2:00 PM PT


def test_format_brief_surfaces_open_commitments(
    mod, gcal_today, prep_alexis, prep_board, tuesday_pacific
):
    prep_lookup = {
        "evt-today-1": prep_alexis,
        "evt-today-2": prep_board,
    }
    body = mod.format_brief(gcal_today["events"], None, prep_lookup, tuesday_pacific)
    # Alexis meeting should note the Q2 roadmap commitment
    assert "Q2 roadmap draft" in body
    # Board meeting has no commitments → no "Open with" line for it
    assert body.count("Open with") == 1


def test_format_brief_tomorrow_preview(mod, gcal_today, tuesday_pacific):
    body = mod.format_brief(gcal_today["events"], None, {}, tuesday_pacific)
    assert "TOMORROW PREVIEW" in body
    assert "Weekly staff sync" in body


def test_format_brief_empty_day(mod, tuesday_pacific):
    body = mod.format_brief([], None, {}, tuesday_pacific)
    assert "quiet" in body.lower() or "no meetings" in body.lower()


def test_format_brief_monday_appends_week_ahead(
    mod, gcal_today, gcal_week, monday_pacific
):
    body = mod.format_brief(
        gcal_today["events"], gcal_week["events"], {}, monday_pacific
    )
    assert "WEEK AHEAD" in body
    assert "Partner intro call" in body
    assert "Board meeting" in body


def test_format_brief_non_monday_omits_week_ahead(
    mod, gcal_today, gcal_week, tuesday_pacific
):
    body = mod.format_brief(
        gcal_today["events"], gcal_week["events"], {}, tuesday_pacific
    )
    assert "WEEK AHEAD" not in body


# ─── run() orchestration ─────────────────────────────────────────────


def test_run_writes_brief_to_cache_file(
    mod, gcal_today, prep_alexis, prep_board, tmp_path, monkeypatch
):
    workspace = tmp_path / "meetings-coach-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "BRIEF_FILE", workspace / "cache" / "morning-brief-ready.txt"
    )
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-morning-meeting.json"
    )

    def fake_run(script, *args, **kw):
        if script == "gcal-fetch.py":
            return gcal_today
        if script == "meeting-prep.py":
            if "evt-today-1" in args:
                return prep_alexis
            if "evt-today-2" in args:
                return prep_board
        return None

    monkeypatch.setattr(mod, "_run_script", fake_run)

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            # Tuesday 2026-04-14 10:30 UTC = 3:30 AM Pacific
            return datetime(2026, 4, 14, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()

    assert result["status"] == "ok"
    brief = workspace / "cache" / "morning-brief-ready.txt"
    assert brief.exists()
    body = brief.read_text(encoding="utf-8")
    assert "Meeting Brief" in body
    assert "Alexis" in body
    assert "Q2 roadmap" in body  # Open commitment surfaced
    # Non-Monday → only 2-day gcal fetch
    assert result.get("weekly_overview_included") is False


def test_run_monday_fetches_week_and_folds(
    mod, gcal_today, gcal_week, tmp_path, monkeypatch
):
    workspace = tmp_path / "meetings-coach-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir()
    monkeypatch.setattr(mod, "WORKSPACE", workspace)
    monkeypatch.setattr(mod, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mod, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mod, "BRIEF_FILE", workspace / "cache" / "morning-brief-ready.txt"
    )
    monkeypatch.setattr(
        mod, "LAST_RUN_FILE", workspace / "cache" / "last-morning-meeting.json"
    )

    calls: list[tuple] = []

    def fake_run(script, *args, **kw):
        calls.append((script, args))
        if script == "gcal-fetch.py" and "7" in args:
            return gcal_week
        if script == "gcal-fetch.py":
            return gcal_today
        return None

    monkeypatch.setattr(mod, "_run_script", fake_run)

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            # Monday 2026-04-20 10:30 UTC = 3:30 AM Pacific, weekday == 0
            return datetime(2026, 4, 20, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert result.get("weekly_overview_included") is True
    body = (workspace / "cache" / "morning-brief-ready.txt").read_text(encoding="utf-8")
    assert "WEEK AHEAD" in body
    # Called gcal-fetch at least twice — once for today, once for week
    gcal_calls = [c for c in calls if c[0] == "gcal-fetch.py"]
    assert len(gcal_calls) >= 2


def test_main_always_exits_zero_on_error(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated")
    monkeypatch.setattr(mod, "run", boom)
    rc = mod.main()
    payload = json.loads(capsys.readouterr().out.strip().split("\n")[-1])
    assert rc == 0
    assert payload["status"] == "error"
