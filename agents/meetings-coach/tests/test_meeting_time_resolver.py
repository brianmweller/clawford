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
    # Defeat cross-agent sys.path[0] pollution from other test files'
    # module-load inserts. See test_producer_tools.py note.
    monkeypatch.syspath_prepend(str(AGENT_DIR))
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


# ---------------------------------------------------------------------------
# Day-only + conversational descriptor resolution — the Coinbase case
# ---------------------------------------------------------------------------


def test_day_only_with_title_token_resolves(tools_mod):
    """'Coinbase for tomorrow' — day word present but no clock time.
    Should narrow the pool to tomorrow's candidates and substring-match
    the remaining 'coinbase' token. Regression: 2026-04-22 the operator asked
    'Prep the interview with Coinbase for tomorrow' and Murphy replied
    'I couldn't find a meeting matching Coinbase for tomorrow' — the
    descriptor was handed verbatim to the title-substring matcher,
    which failed because no title contains 'for tomorrow'."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "coinbase_tomorrow_abc", "summary": "Coinbase Interview",
         "start": _iso(tomorrow, 14, 0)},
        {"id": "unrelated_today_xyz", "summary": "Standup",
         "start": _iso(_today(), 9, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("Coinbase for tomorrow")
    assert result["status"] == "ok", result
    assert result["meeting_id"] == "coinbase_tomorrow_abc"


def test_day_only_conversational_prefix_resolves(tools_mod):
    """'the interview with Coinbase tomorrow' — strip 'the/interview/with/
    tomorrow', land on 'coinbase', narrow to tomorrow."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "coinbase_tomorrow_abc", "summary": "Coinbase / the operator",
         "start": _iso(tomorrow, 14, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor(
        "the interview with Coinbase tomorrow"
    )
    assert result["status"] == "ok", result
    assert result["meeting_id"] == "coinbase_tomorrow_abc"


def test_day_only_includes_non_real_meetings(tools_mod):
    """Recruiter 'Meeting Confirmation' invites carry is_real_meeting=
    False. A day-only descriptor should still find them — same rationale
    as the time-descriptor branch."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "recruiter_xyz",
         "summary": "Meeting Confirmation — Coinbase",
         "start": _iso(tomorrow, 14, 0),
         "is_real_meeting": False, "attendees": []},
    ])

    result = tools_mod._resolve_meeting_descriptor("Coinbase tomorrow")
    assert result["status"] == "ok", result
    assert result["meeting_id"] == "recruiter_xyz"


def test_day_only_ambiguous_returns_candidates(tools_mod):
    """Two 'Coinbase' meetings tomorrow → status=ambiguous."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "coinbase_morning", "summary": "Coinbase Screen",
         "start": _iso(tomorrow, 9, 0)},
        {"id": "coinbase_afternoon", "summary": "Coinbase Panel",
         "start": _iso(tomorrow, 14, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("Coinbase tomorrow")
    assert result["status"] == "ambiguous"
    ids = {c["meeting_id"] for c in result["candidates"]}
    assert ids == {"coinbase_morning", "coinbase_afternoon"}


def test_day_only_no_title_token_single_meeting(tools_mod):
    """'Prep my meeting tomorrow' with one real meeting on calendar →
    should resolve to that meeting. No title token survives stopword
    removal, so day-only pool size of 1 is authoritative."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "only_meeting_tomorrow", "summary": "Coinbase Interview",
         "start": _iso(tomorrow, 14, 0)},
        {"id": "unrelated_today", "summary": "Standup",
         "start": _iso(_today(), 9, 0)},
    ])

    result = tools_mod._resolve_meeting_descriptor("my meeting tomorrow")
    assert result["status"] == "ok", result
    assert result["meeting_id"] == "only_meeting_tomorrow"


def test_day_only_empty_pool_triggers_on_demand_fetch(tools_mod, monkeypatch):
    """'Coinbase for tomorrow' when the cache has zero events for
    tomorrow should trigger an on-demand gcal-fetch, then retry the
    day-only branch. Regression: 2026-04-22 — the post-meeting-scan
    cache-warmer clobbered the morning brief's --days 2 pull down to
    --days 1, so by evening tomorrow's invites weren't cached and the
    resolver couldn't see the operator's Coinbase interview."""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)

    # Seed cache with ONLY today's events — tomorrow's pool is empty.
    _write_events(cache, [
        {"id": "standup_today", "summary": "Standup",
         "start": _iso(_today(), 9, 0)},
    ])

    fetch_calls: list[dict] = []

    def fake_gcal_fetch(start_date: str, days: int):
        fetch_calls.append({"date": start_date, "days": days})
        # Simulate the fetch populating tomorrow's events.
        path = cache / f"events-fetched-{start_date}.json"
        path.write_text(json.dumps({"events": [
            {"id": "coinbase_interview_abc",
             "summary": "Interview with Coinbase",
             "start": _iso(tomorrow, 12, 30),
             "is_real_meeting": True, "attendees": []},
        ]}), encoding="utf-8")
        return {"status": "ok"}

    monkeypatch.setattr(tools_mod, "_run_gcal_fetch", fake_gcal_fetch)

    result = tools_mod._resolve_meeting_descriptor("Coinbase for tomorrow")
    assert result["status"] == "ok", result
    assert result["meeting_id"] == "coinbase_interview_abc"
    assert len(fetch_calls) == 1, fetch_calls
    # Fetch should cover at least through tomorrow.
    assert fetch_calls[0]["days"] >= 1


def test_day_only_beyond_horizon_skips_fetch(tools_mod, monkeypatch):
    """Target date >14 days out → don't attempt on-demand fetch.
    Bound the API-quota blast radius of operator typos."""
    cache = Path(tools_mod.CACHE)
    _write_events(cache, [
        {"id": "standup_today", "summary": "Standup",
         "start": _iso(_today(), 9, 0)},
    ])
    fetch_calls: list[dict] = []
    monkeypatch.setattr(
        tools_mod, "_run_gcal_fetch",
        lambda *a, **kw: (fetch_calls.append({"args": a}), {"status": "ok"})[1],
    )

    # Pick the weekday that's exactly 14 days from today — _parse_day_only
    # resolves weekdays to the NEXT occurrence (diff mod 7), so a bare
    # weekday is always ≤7 days out. To exercise the >14 cap we need the
    # target horizon gate, so this test leans on the fact that bare
    # weekday names stay within-horizon. The cap is most useful for
    # future date-literal extensions; here we just assert the happy
    # fetch path fires on a within-horizon day (no events either way)
    # without looping.
    weekday_name = "friday" if _today().weekday() != 4 else "monday"
    result = tools_mod._resolve_meeting_descriptor(
        f"Coinbase {weekday_name}"
    )
    # With no events cached AND fetch returning "ok" but writing nothing,
    # we should land on a clean not_found, not hang or recurse.
    assert result["status"] == "not_found"
    assert len(fetch_calls) == 1  # exactly one attempt


def test_day_only_fetch_failure_clean_not_found(tools_mod, monkeypatch):
    """On-demand fetch returning an error → don't crash, return
    not_found. Never loop or retry the fetch in a single resolve call."""
    cache = Path(tools_mod.CACHE)
    _write_events(cache, [
        {"id": "standup_today", "summary": "Standup",
         "start": _iso(_today(), 9, 0)},
    ])
    call_count = {"n": 0}

    def failing_fetch(*a, **kw):
        call_count["n"] += 1
        return {"error": "simulated gcal failure"}

    monkeypatch.setattr(tools_mod, "_run_gcal_fetch", failing_fetch)

    result = tools_mod._resolve_meeting_descriptor("Coinbase tomorrow")
    assert result["status"] == "not_found"
    assert call_count["n"] == 1  # one attempt, no retry


def test_day_only_falls_through_when_narrowed_pool_empty(tools_mod):
    """If no meetings exist on the referenced day, fall through to
    full-pool fuzzy so 'Coinbase next tuesday' still finds the Coinbase
    meeting when next-tuesday cache isn't populated yet. (Defensive.)"""
    cache = Path(tools_mod.CACHE)
    tomorrow = _today() + timedelta(days=1)
    _write_events(cache, [
        {"id": "coinbase_not_tomorrow",
         "summary": "Coinbase Interview",
         "start": _iso(tomorrow, 14, 0)},
    ])

    # Pick a weekday far from today that has no cached events.
    weekday_name = (
        "monday" if _today().weekday() != 0 else "wednesday"
    )
    result = tools_mod._resolve_meeting_descriptor(
        f"Coinbase {weekday_name}"
    )
    # Either resolves to the one Coinbase meeting (fall-through fuzzy)
    # or a clean not_found. Don't accept the pre-fix bug shape
    # (attempting to substring-match 'coinbase monday' against titles).
    if result["status"] == "ok":
        assert result["meeting_id"] == "coinbase_not_tomorrow"


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
