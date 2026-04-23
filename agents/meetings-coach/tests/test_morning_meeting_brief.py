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


def test_format_brief_renders_professional_prep_when_meeting_type_set(
    mod, gcal_today, tuesday_pacific
):
    """A meeting with meeting_type='recruiter-screen' and llm_prep should
    render the professional block instead of (or alongside) the generic
    commit summary."""
    prep = {
        "meetings": [{
            "meeting_id": "evt-today-1",
            "meeting_type": "recruiter-screen",
            "self_context": {
                "target_company": {"company": "Anthropic", "tier_company": "A"},
                "active_pipeline_stage": {"stage": "hiring-manager"},
            },
            "llm_prep": {
                "prep_summary": "First recruiter screen with a frontier AI lab.",
                "recipient_model": "Michelle needs to advance the operator to hiring manager.",
                "objective": "Evaluate match + pitch fit if it clears the bar.",
                "fit_pitch": "Led marketplace causal-science orgs across Example Corp and LinkedIn.",
                "compelling_angle": "AI safety + large-scale inference is a natural extension.",
                "fit_evidence": [
                    "Shipped marketplace-scale causal systems at prior role",
                    "Authored FLEX operating model at LinkedIn",
                    "Decade of experience owning exec-adjacent levers",
                ],
                "evaluation_questions": [
                    "What's the team's current roadmap priority?",
                    "How does the role interact with Anthropic's safety org?",
                    "What's the expected scope for the first 90 days?",
                ],
                "red_flags": ["Unclear reporting line in the inbound"],
            },
            "context": {},
        }],
    }
    prep_lookup = {"evt-today-1": prep}
    body = mod.format_brief(gcal_today["events"], None, prep_lookup, tuesday_pacific)

    # Compact header: type + target + stage + one-line summary + pointer.
    assert "recruiter-screen" in body
    assert "Anthropic" in body
    assert "hiring-manager" in body
    assert "First recruiter screen" in body
    assert "Full prep in Workflowy" in body

    # Full prep block MUST stay out of the brief — Workflowy owns it.
    # Regression guard for the 2026-04-23 verbose-brief report.
    assert "Michelle needs" not in body
    assert "marketplace causal-science" not in body
    assert "safety org" not in body
    assert "FLEX operating model" not in body
    assert "reporting line" not in body
    assert "Pitch:" not in body
    assert "Drop-in evidence" not in body
    assert "Ask:" not in body
    assert "Red flags" not in body


def test_format_brief_general_meeting_keeps_existing_shape(
    mod, gcal_today, tuesday_pacific
):
    """meeting_type='general' (or missing) → no professional block rendered."""
    prep = {
        "meetings": [{
            "meeting_id": "evt-today-1",
            "meeting_type": "general",
            "context": {"commitments": []},
        }],
    }
    body = mod.format_brief(
        gcal_today["events"], None, {"evt-today-1": prep}, tuesday_pacific,
    )
    assert "recruiter-screen" not in body
    assert "Ask:" not in body
    assert "Red flags" not in body


def test_format_brief_falls_through_to_header_only_on_llm_prep_error(
    mod, gcal_today, tuesday_pacific
):
    """When llm_prep has an 'error' key, render the header + self_context
    only (no Ask/Surface/Red flags) and let the generic commit summary
    (if any) render below."""
    prep = {
        "meetings": [{
            "meeting_id": "evt-today-1",
            "meeting_type": "recruiter-screen",
            "self_context": {"target_company": None, "active_pipeline_stage": None},
            "llm_prep": {"error": "timeout"},
            "context": {"commitments": []},
        }],
    }
    body = mod.format_brief(
        gcal_today["events"], None, {"evt-today-1": prep}, tuesday_pacific,
    )
    assert "recruiter-screen" in body
    assert "Ask:" not in body
    assert "Surface:" not in body


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


def test_run_auto_pushes_professional_prep_to_workflowy(
    mod, gcal_today, prep_alexis, prep_board, tmp_path, monkeypatch
):
    """When a meeting's prep carries meeting_type != 'general' (i.e.,
    recruiter-screen / hiring-manager / hiring-panel), the morning-brief
    orchestrator must auto-push that prep to Workflowy. General
    meetings are NOT pushed — prep belongs only on professional ones."""
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

    # Fabricate a professional prep result for evt-today-1 (Alexis
    # meeting) and leave evt-today-2 (Board) as general.
    prep_recruiter_screen = {
        "meetings": [{
            **(prep_alexis["meetings"][0]),
            "meeting_type": "recruiter-screen",
            "self_context": {"target_company": {"company": "Anthropic",
                                                "tier_company": "A"}},
            "llm_prep": {
                "prep_summary": "Screen with Anthropic recruiter",
                "recipient_model": "They want a fit signal + scheduling yes/no",
                "objective": "Evaluate + pitch",
                "compelling_angle": "AI safety + large-scale inference",
                "fit_pitch": "Marketplace causal-ML orgs at scale.",
                "fit_evidence": ["e1", "e2", "e3"],
                "evaluation_questions": ["q1", "q2", "q3"],
                "red_flags": ["r1"],
            },
        }],
    }

    pushed_eids: list[str] = []

    def fake_run(script, *args, **kw):
        if script == "gcal-fetch.py":
            return gcal_today
        if script == "meeting-prep.py":
            if "evt-today-1" in args:
                return prep_recruiter_screen
            if "evt-today-2" in args:
                return prep_board  # meeting_type absent → 'general'
        if script == "workflowy-sync.py":
            # --push-prep-meeting EVENT_ID
            if "--push-prep-meeting" in args:
                idx = args.index("--push-prep-meeting")
                if idx + 1 < len(args):
                    pushed_eids.append(args[idx + 1])
            return {"status": "ok"}
        return None

    monkeypatch.setattr(mod, "_run_script", fake_run)

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 14, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()

    assert result["status"] == "ok"
    # Only the recruiter-screen meeting (evt-today-1) gets auto-pushed.
    assert pushed_eids == ["evt-today-1"]


def test_run_auto_push_general_meetings_are_skipped(
    mod, gcal_today, prep_alexis, prep_board, tmp_path, monkeypatch
):
    """If every meeting is classified 'general', no Workflowy push
    fires at all — prep is only for professional meetings."""
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

    pushed_eids: list[str] = []

    def fake_run(script, *args, **kw):
        if script == "gcal-fetch.py":
            return gcal_today
        if script == "meeting-prep.py":
            return prep_alexis if "evt-today-1" in args else prep_board
        if script == "workflowy-sync.py" and "--push-prep-meeting" in args:
            pushed_eids.append("unexpected")
        return None

    monkeypatch.setattr(mod, "_run_script", fake_run)

    class _Dt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 14, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, "datetime", _Dt)

    result = mod.run()
    assert result["status"] == "ok"
    assert pushed_eids == [], "general meetings should NOT push"


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
