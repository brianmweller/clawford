"""Tests for agents/family-calendar/scripts/morning-briefing.py.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`family-calendar:morning-briefing`. Runs gcal-fetch.py, formats the
brief per CRONS.md (time-blocked today section + tomorrow preview, with
a weekly overview appended on Mondays), and writes to
cache/morning-brief-ready.txt for the 5 AM PT fleet delivery path.

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
SCRIPT = REPO_ROOT / "agents" / "family-calendar" / "scripts" / "morning-briefing.py"
FIXTURES = Path(__file__).parent / "fixtures" / "morning-briefing"


def _load_module():
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mb():
    return _load_module()


@pytest.fixture
def gcal_2d():
    with open(FIXTURES / "gcal-fetch-2d.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def gcal_7d():
    with open(FIXTURES / "gcal-fetch-7d.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def tuesday_pacific():
    """2026-04-14 03:00 Pacific = Tuesday morning (not Monday → no
    weekly-overview section)."""
    return datetime(2026, 4, 14, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


@pytest.fixture
def monday_pacific():
    """2026-04-20 03:00 Pacific = Monday morning (weekday == 0 → weekly
    overview section appended)."""
    return datetime(2026, 4, 20, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


# ─── split_today_tomorrow ────────────────────────────────────────────


def test_split_today_tomorrow_partitions_by_pacific_date(
    mb, gcal_2d, tuesday_pacific
):
    today_events, tomorrow_events = mb.split_today_tomorrow(
        gcal_2d.get("events", []), tuesday_pacific
    )
    assert len(today_events) == 3
    assert len(tomorrow_events) == 1
    # today contains dentist + ballet + dinner (substring match to
    # avoid the em-dash character entirely — cp1252 stdout traps)
    today_summaries = {e["summary"] for e in today_events}
    assert any("Dentist" in s for s in today_summaries)
    assert "Avery ballet" in today_summaries
    assert "Family dinner with grandparents" in today_summaries
    # tomorrow contains the field trip
    assert tomorrow_events[0]["summary"] == "Avery preschool field trip"


# ─── time-block grouping ─────────────────────────────────────────────


def test_group_by_time_block_morning_afternoon_evening(mb, gcal_2d, tuesday_pacific):
    today_events, _ = mb.split_today_tomorrow(
        gcal_2d.get("events", []), tuesday_pacific
    )
    grouped = mb.group_by_time_block(today_events)
    assert len(grouped["morning"]) == 1
    assert "Dentist" in grouped["morning"][0]["summary"]
    assert [e["summary"] for e in grouped["afternoon"]] == ["Avery ballet"]
    assert [e["summary"] for e in grouped["evening"]] == [
        "Family dinner with grandparents"
    ]


def test_group_by_time_block_boundaries(mb):
    """Morning: start hour < 12. Afternoon: 12-16. Evening: 17+."""
    events = [
        {"summary": "Late morning", "start": "2026-04-14T11:59:00-07:00"},
        {"summary": "Noon", "start": "2026-04-14T12:00:00-07:00"},
        {"summary": "Late afternoon", "start": "2026-04-14T16:59:00-07:00"},
        {"summary": "Early evening", "start": "2026-04-14T17:00:00-07:00"},
    ]
    grouped = mb.group_by_time_block(events)
    assert [e["summary"] for e in grouped["morning"]] == ["Late morning"]
    assert [e["summary"] for e in grouped["afternoon"]] == ["Noon", "Late afternoon"]
    assert [e["summary"] for e in grouped["evening"]] == ["Early evening"]


# ─── format_brief ────────────────────────────────────────────────────


def test_format_brief_includes_header_and_time_blocks(
    mb, gcal_2d, tuesday_pacific
):
    events = gcal_2d.get("events", [])
    body = mb.format_brief(events, None, tuesday_pacific)

    assert "Family Day" in body
    assert "Tuesday, April 14" in body
    assert "MORNING" in body
    assert "Dentist" in body
    assert "AFTERNOON" in body
    assert "Avery ballet" in body
    assert "EVENING" in body
    assert "Family dinner with grandparents" in body

    # Tomorrow preview
    assert "TOMORROW PREVIEW" in body
    assert "Avery preschool field trip" in body


def test_format_brief_uses_calendar_emoji(mb, gcal_2d, tuesday_pacific):
    events = gcal_2d.get("events", [])
    body = mb.format_brief(events, None, tuesday_pacific)
    # 👨 U+1F468 and 🧒 U+1F9D2 — check by hex escape so cp1252
    # source-decode woes don't affect the assertions.
    assert "\U0001f468" in body  # Sam
    assert "\U0001f9d2" in body  # Avery


def test_format_brief_includes_location_when_present(
    mb, gcal_2d, tuesday_pacific
):
    events = gcal_2d.get("events", [])
    body = mb.format_brief(events, None, tuesday_pacific)
    assert "Palo Alto Dental" in body
    assert "Example Ballet Studio" in body


def test_format_brief_renders_times_in_pacific(mb, gcal_2d, tuesday_pacific):
    """Start times are in ISO form with -07:00 offset. Brief should
    render them in 12-hour Pacific format (9:00 AM, 3:30 PM, 6:30 PM)."""
    events = gcal_2d.get("events", [])
    body = mb.format_brief(events, None, tuesday_pacific)
    # 9:00 dentist
    assert "9:00 AM" in body
    # 3:30 ballet
    assert "3:30 PM" in body
    # 6:30 dinner
    assert "6:30 PM" in body


def test_format_brief_standard_day_shape_when_no_events(mb, tuesday_pacific):
    """No today events → short 'Standard weekday' message. Tomorrow
    preview still present."""
    body = mb.format_brief([], None, tuesday_pacific)
    assert "Standard Tuesday — no exceptions" in body


def test_format_brief_includes_weekly_overview_on_monday(
    mb, gcal_2d, gcal_7d, monday_pacific
):
    """On Mondays the brief appends a weekly overview section built
    from the 7-day gcal-fetch output."""
    body = mb.format_brief(
        gcal_2d.get("events", []), gcal_7d.get("events", []), monday_pacific
    )
    assert "📅 WEEK AHEAD" in body
    # Monday entries
    assert "Avery swim lesson" in body
    # Wednesday entries
    assert "Dentist follow-up" in body
    # Friday entries
    assert "School early dismissal" in body


def test_format_brief_omits_weekly_overview_on_non_monday(
    mb, gcal_2d, gcal_7d, tuesday_pacific
):
    """Passing week_events on a non-Monday should NOT produce the
    section — run() only fetches --days 7 on Mondays, but format_brief
    is defensive."""
    body = mb.format_brief(
        gcal_2d.get("events", []), gcal_7d.get("events", []), tuesday_pacific
    )
    assert "📅 WEEK AHEAD" not in body


def test_format_brief_empty_tomorrow_says_standard_weekday(
    mb, tuesday_pacific
):
    """If tomorrow has zero events, the TOMORROW PREVIEW line says so."""
    events = [
        {
            "id": "today-1",
            "summary": "Dentist",
            "start": "2026-04-14T09:00:00-07:00",
            "calendar_emoji": "👨",
            "calendar_label": "Sam",
        },
    ]
    body = mb.format_brief(events, None, tuesday_pacific)
    assert "📋 TOMORROW PREVIEW" in body
    assert "Standard" in body  # "Standard Wednesday — no exceptions" etc.


# ─── run() orchestration ─────────────────────────────────────────────


def test_run_writes_brief_to_cache_file(
    mb, gcal_2d, tmp_path, monkeypatch
):
    workspace = tmp_path / "family-calendar-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mb, "WORKSPACE", workspace)
    monkeypatch.setattr(mb, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mb, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mb, "BRIEF_FILE", workspace / "cache" / "morning-brief-ready.txt"
    )
    monkeypatch.setattr(
        mb, "LAST_RUN_FILE", workspace / "cache" / "last-morning-brief.json"
    )

    subs_args: list[tuple] = []

    def fake_run_script(script_name, *args, timeout=120):
        subs_args.append(args)
        if "--days" in args and "2" in args:
            return gcal_2d
        return None

    monkeypatch.setattr(mb, "_run_script", fake_run_script)

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            # Tuesday 2026-04-14 10:30 UTC = 3:30 AM Pacific
            return datetime(2026, 4, 14, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mb, "datetime", _FrozenDt)

    result = mb.run()

    assert result["status"] == "ok"
    brief_path = workspace / "cache" / "morning-brief-ready.txt"
    assert brief_path.exists()
    body = brief_path.read_text(encoding="utf-8")
    assert "🐭📅 Family Day" in body
    # Non-Monday → no weekly section → should have only called gcal-fetch once
    assert len(subs_args) == 1


def test_run_fetches_week_on_monday(mb, gcal_2d, gcal_7d, tmp_path, monkeypatch):
    workspace = tmp_path / "family-calendar-workspace"
    (workspace / "cache").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    monkeypatch.setattr(mb, "WORKSPACE", workspace)
    monkeypatch.setattr(mb, "CACHE_DIR", workspace / "cache")
    monkeypatch.setattr(mb, "SCRIPTS_DIR", workspace / "scripts")
    monkeypatch.setattr(
        mb, "BRIEF_FILE", workspace / "cache" / "morning-brief-ready.txt"
    )
    monkeypatch.setattr(
        mb, "LAST_RUN_FILE", workspace / "cache" / "last-morning-brief.json"
    )

    subs_args: list[tuple] = []

    def fake_run_script(script_name, *args, timeout=120):
        subs_args.append(args)
        if "2" in args:
            return gcal_2d
        if "7" in args:
            return gcal_7d
        return None

    monkeypatch.setattr(mb, "_run_script", fake_run_script)

    class _FrozenDt(datetime):
        @classmethod
        def now(cls, tz=None):
            # Monday 2026-04-20 10:30 UTC = 3:30 AM Pacific, weekday == 0
            return datetime(2026, 4, 20, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(mb, "datetime", _FrozenDt)

    result = mb.run()

    assert result["status"] == "ok"
    assert result.get("weekly_overview_included") is True
    # Called gcal-fetch twice (2d + 7d)
    assert len(subs_args) == 2
    body = (workspace / "cache" / "morning-brief-ready.txt").read_text(encoding="utf-8")
    assert "📅 WEEK AHEAD" in body


def test_main_always_exits_zero_on_error(mb, monkeypatch, capsys):
    def boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(mb, "run", boom)

    rc = mb.main()
    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out.strip().split("\n")[-1])
    assert payload["status"] == "error"
    assert "simulated failure" in payload["error"]
    assert payload["alert"].startswith("🐭")
