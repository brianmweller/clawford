"""Time-descriptor resolution for prep_meeting / force_prep.

Operators say 'tomorrow 2:45pm', not 'event_id _8524ugilad03...'. The
meeting resolver accepts time references alongside name/email/title,
matching candidate events by start timestamp within a ±5 min window.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(AGENT_DIR))


TZ = ZoneInfo("America/Los_Angeles")


def _today() -> date:
    return datetime.now(TZ).date()


def _iso(dt_date: date, hour: int, minute: int = 0) -> str:
    dt = datetime(dt_date.year, dt_date.month, dt_date.day,
                  hour, minute, tzinfo=TZ)
    return dt.isoformat()


def _write_events(cache_dir: Path, events: list[dict]) -> None:
    """Write an events cache file in gcal-fetch shape. One file per
    (arbitrary) date key — the loader walks events-*.json and dedupes
    by id, so the filename itself doesn't matter for resolution."""
    for ev in events:
        ev.setdefault("is_real_meeting", True)
        ev.setdefault("attendees", [])
    payload = {"events": events}
    path = cache_dir / f"events-{_today().isoformat()}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def tools_mod(tmp_path, monkeypatch):
    workspace = tmp_path / "meetings-coach-workspace"
    workspace.mkdir()
    cache = workspace / "cache"
    cache.mkdir()
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    for mod in list(sys.modules):
        if mod == "tools":
            del sys.modules[mod]
    import tools
    monkeypatch.setattr(tools, "WORKSPACE", str(workspace))
    monkeypatch.setattr(tools, "CACHE", str(cache))
    return tools


# ---------------------------------------------------------------------------
# Time-descriptor resolution — the Alyssa/Adobe case
# ---------------------------------------------------------------------------


def test_tomorrow_time_resolves_single_match(tools_mod):
    """'tomorrow 2:45pm' uniquely identifies one event → status=ok."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "adobe_interview_abc123", "summary": "Meeting Confirmation",
         "start": _iso(tomorrow, 14, 45)},
        {"id": "unrelated_morning_xyz", "summary": "Standup",
         "start": _iso(tomorrow, 9, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("tomorrow 2:45pm")
    assert result["status"] == "ok", result
    assert result["meeting_id"] == "adobe_interview_abc123"


def test_time_before_day_phrase_resolves(tools_mod):
    """'2:45pm tomorrow' — word order shouldn't matter."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "adobe_interview_abc123", "summary": "Interview",
         "start": _iso(tomorrow, 14, 45)},
    ])

    result = tools_mod._resolve_meeting_descriptor("2:45pm tomorrow")
    assert result["status"] == "ok"
    assert result["meeting_id"] == "adobe_interview_abc123"


def test_tomorrow_at_time_resolves(tools_mod):
    """'tomorrow at 2:45pm' — 'at' is a filler word."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "adobe_interview_abc123", "summary": "Interview",
         "start": _iso(tomorrow, 14, 45)},
    ])

    result = tools_mod._resolve_meeting_descriptor("tomorrow at 2:45pm")
    assert result["status"] == "ok"
    assert result["meeting_id"] == "adobe_interview_abc123"


def test_today_time_resolves(tools_mod):
    cache = Path(tools_mod.CACHE)
    today = _today()
    _write_events(cache, [
        {"id": "standup_today_111", "summary": "Standup",
         "start": _iso(today, 15, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("3pm today")
    assert result["status"] == "ok"
    assert result["meeting_id"] == "standup_today_111"


def test_24h_format_resolves(tools_mod):
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "meeting_abc", "summary": "Meeting",
         "start": _iso(tomorrow, 14, 45)},
    ])

    result = tools_mod._resolve_meeting_descriptor("14:45 tomorrow")
    assert result["status"] == "ok"
    assert result["meeting_id"] == "meeting_abc"


def test_hour_only_pm(tools_mod):
    """'3pm today' — no minutes on the time is fine (implicit :00)."""
    cache = Path(tools_mod.CACHE)
    today = _today()
    _write_events(cache, [
        {"id": "three_pm_block", "summary": "Block",
         "start": _iso(today, 15, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("3pm today")
    assert result["status"] == "ok"


def test_bare_time_defaults_to_today(tools_mod):
    """'3pm' with no day → assume today's meeting."""
    cache = Path(tools_mod.CACHE)
    today = _today()
    _write_events(cache, [
        {"id": "three_pm_today", "summary": "Block",
         "start": _iso(today, 15, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("3pm")
    assert result["status"] == "ok"
    assert result["meeting_id"] == "three_pm_today"


def test_time_window_matches_within_five_min(tools_mod):
    """'3pm' matches a 3:03pm meeting (within the 5-min tolerance) —
    operators round to the marquee time."""
    cache = Path(tools_mod.CACHE)
    today = _today()
    _write_events(cache, [
        {"id": "three_oh_three", "summary": "Block",
         "start": _iso(today, 15, 3)},
    ])

    result = tools_mod._resolve_meeting_descriptor("3pm")
    assert result["status"] == "ok"
    assert result["meeting_id"] == "three_oh_three"


def test_ambiguous_time_returns_candidates(tools_mod):
    """Two events at the same time → status=ambiguous with candidates."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "first_2pm_aaa", "summary": "Thing A",
         "start": _iso(tomorrow, 14, 0)},
        {"id": "second_2pm_bbb", "summary": "Thing B",
         "start": _iso(tomorrow, 14, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("tomorrow 2pm")
    assert result["status"] == "ambiguous"
    ids = {c["meeting_id"] for c in result["candidates"]}
    assert ids == {"first_2pm_aaa", "second_2pm_bbb"}


def test_time_no_match_returns_not_found(tools_mod):
    """Parsed a time, but no event in pool matches → not_found with a
    time-specific reason (more useful than substring fallback)."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "standup_morning", "summary": "Standup",
         "start": _iso(tomorrow, 9, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("tomorrow 5pm")
    assert result["status"] == "not_found"
    assert "5" in result.get("reason", "") or "17" in result.get("reason", "")


def test_plain_name_still_resolves(tools_mod):
    """Regression guard: the time-descriptor branch mustn't break plain
    name / email / title matching."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "alyssa_call_xyz", "summary": "Alyssa / the operator",
         "start": _iso(tomorrow, 14, 45),
         "attendees": [{"name": "Alyssa Bonefas",
                        "email": "alyssa@example.com"}]},
    ])

    result = tools_mod._resolve_meeting_descriptor("Alyssa")
    assert result["status"] == "ok"
    assert result["meeting_id"] == "alyssa_call_xyz"


def test_time_descriptor_overrides_is_real_meeting_filter(tools_mod):
    """Recruiter invites (Adobe, Greenhouse, etc.) often carry
    is_real_meeting=false because no attendees are explicit on the
    invite — the interviewer lives only in the description body, and
    the 'location' is a phone code not a video link. When the operator
    names a specific clock time, trust them: include these events in
    the candidate pool regardless of the classifier verdict."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "recruiter_invite_xyz",
         "summary": "Meeting Confirmation - Sam Smith",
         "start": _iso(tomorrow, 14, 45),
         "is_real_meeting": False,
         "attendees": []},
    ])

    result = tools_mod._resolve_meeting_descriptor("tomorrow 2:45pm")
    assert result["status"] == "ok", result
    assert result["meeting_id"] == "recruiter_invite_xyz"


def test_plain_name_still_filters_noise(tools_mod):
    """Complement to above: plain-name / title paths still honor
    is_real_meeting=false (stops 'cleaners arrive'-type events from
    polluting fuzzy substring matches)."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "not_real_xyz", "summary": "Cleaners arrive",
         "start": _iso(tomorrow, 13, 30),
         "is_real_meeting": False, "attendees": []},
    ])

    result = tools_mod._resolve_meeting_descriptor("cleaners")
    assert result["status"] == "not_found"


def test_prep_meeting_accepts_time_descriptor(tools_mod, monkeypatch):
    """End-to-end: the LLM calls prep_meeting('tomorrow 2:45pm') and
    it shells out with the resolved event id."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "adobe_interview_abc123", "summary": "Meeting Confirmation",
         "start": _iso(tomorrow, 14, 45)},
    ])

    captured: dict = {}

    def fake_run(script_path, *args, **kwargs):
        captured["script"] = script_path
        captured["args"] = list(args)
        return {"status": "ok", "meeting_id": "adobe_interview_abc123"}

    import subprocess_helpers  # type: ignore
    monkeypatch.setattr(subprocess_helpers, "run_json_script", fake_run)
    monkeypatch.setattr(subprocess_helpers, "is_subprocess_error",
                        lambda r: False)

    result = tools_mod.force_prep("tomorrow 2:45pm")
    assert result["status"] == "ok"
    idx = captured["args"].index("--meeting-id")
    assert captured["args"][idx + 1] == "adobe_interview_abc123"
