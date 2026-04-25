"""Tests for agents/shared/scripts/calendar-brain-listener.py.

The listener daemon that keeps the shared brain fresh via 60s
incremental polling. Tests cover the pure tick logic (with fake
Calendar services) — the main-loop + signal handling shell is not
unit-tested; integration lives in the Phase 2 simulation.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.shared.calendar_brain import (
    atomic_write_brain,
    read_brain,
    read_sync_tokens,
    write_sync_tokens,
)

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "calendar-brain-listener.py"
)
_spec = importlib.util.spec_from_file_location("calendar_brain_listener", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class _FakeService:
    """Stand-in Calendar service. ``responses`` maps query signatures to
    response dicts. Unrecognized queries return 404-ish empty."""

    def __init__(self, responses, raise_410_for=None):
        self.responses = responses
        self.raise_410_for = raise_410_for or set()
        self.calls = []

    def events(self):
        return self

    def list(self, **kwargs):
        self.calls.append(dict(kwargs))
        return _Exec(kwargs, self.responses, self.raise_410_for)


class _Exec:
    def __init__(self, kwargs, responses, raise_410_for):
        self.kwargs = kwargs
        self.responses = responses
        self.raise_410_for = raise_410_for

    def execute(self):
        if self.kwargs.get("syncToken") in self.raise_410_for:
            # family-calendar tests install bare-ModuleType stubs for
            # googleapiclient at sys.modules and don't clean up. Pop
            # them so the real errors submodule resolves below. See
            # test_calendar_fetch.py for the same dance.
            import sys as _sys
            for _m in list(_sys.modules):
                if _m == "googleapiclient" or _m.startswith("googleapiclient."):
                    del _sys.modules[_m]
            from googleapiclient.errors import HttpError
            from unittest.mock import MagicMock
            resp = MagicMock()
            resp.status = 410
            resp.reason = "Gone"
            raise HttpError(resp=resp, content=b'')
        key = self.kwargs.get("syncToken") or "FULL"
        return self.responses.get(key, {"items": []})


def _raw_new_event(eid, start, summary="Fresh event"):
    return {
        "id": eid,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": start},
        "status": "confirmed",
        "hangoutLink": "https://meet.google.com/fresh",
    }


def _raw_cancelled(eid, start="2026-04-23T10:00:00-07:00"):
    return {
        "id": eid,
        "summary": "gone",
        "start": {"dateTime": start},
        "end": {"dateTime": start},
        "status": "cancelled",
    }


# ---------------------------------------------------------------------------
# Tests for the pure tick logic
# ---------------------------------------------------------------------------


def _seed_brain(path, events):
    atomic_write_brain(path, {
        "generated_at": "2026-04-23T00:00:00+00:00",
        "fetched_via": "daily-build",
        "window": {"time_min": "2026-04-23T00:00:00-07:00",
                   "time_max": "2026-05-01T00:00:00-07:00"},
        "events": events,
    })


def _seed_tokens(path, tokens):
    write_sync_tokens(path, tokens)


def test_tick_applies_delta_adds_new_event(tmp_path):
    """Incremental fetch returns a new event → brain gains it."""
    brain_path = tmp_path / "brain.json"
    tokens_path = tmp_path / "tokens.json"
    _seed_brain(brain_path, [])
    _seed_tokens(tokens_path, {
        "cal1": {"sync_token": "v1", "last_success_at": "seed", "last_error": None},
    })

    svc = _FakeService({
        "v1": {
            "items": [_raw_new_event("fresh_1", "2026-04-23T14:00:00-07:00")],
            "nextSyncToken": "v2",
        },
    })

    summary = _mod.run_tick(
        service=svc,
        brain_path=brain_path,
        tokens_path=tokens_path,
        calendars=[{"id": "cal1", "label": "Cal One", "emoji": ""}],
        workflowy_event_ids=set(),
        skip_titles=[],
        lookahead_days=8,
        now_utc=datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc),
    )
    assert summary["status"] == "ok"

    brain = read_brain(brain_path)
    assert [e["id"] for e in brain["events"]] == ["fresh_1"]
    assert brain["fetched_via"] == "listener"

    tokens = read_sync_tokens(tokens_path)
    assert tokens["cal1"]["sync_token"] == "v2"
    assert tokens["cal1"]["last_error"] is None


def test_tick_applies_delta_removes_cancelled(tmp_path):
    """Incremental fetch returning status=cancelled for an existing
    event → brain drops it."""
    brain_path = tmp_path / "brain.json"
    tokens_path = tmp_path / "tokens.json"
    # Seed with an existing normalized event matching id "evt_a"
    _seed_brain(brain_path, [{
        "id": "evt_a",
        "calendar_id": "cal1",
        "calendar_label": "Cal One",
        "calendar_emoji": "",
        "summary": "Old event",
        "start": "2026-04-23T09:00:00-07:00",
        "end": "2026-04-23T09:30:00-07:00",
        "all_day": False,
        "location": "",
        "description": "",
        "status": "confirmed",
        "attendees": [],
        "organizer": "",
        "conference_link": "",
        "has_video_link": True,
        "in_workflowy": False,
        "is_meeting": True,
        "owner": "sergeant-murphy",
        "is_real_meeting": True,
    }])
    _seed_tokens(tokens_path, {
        "cal1": {"sync_token": "v1", "last_success_at": "seed", "last_error": None},
    })

    svc = _FakeService({
        "v1": {"items": [_raw_cancelled("evt_a")], "nextSyncToken": "v2"},
    })

    _mod.run_tick(
        service=svc,
        brain_path=brain_path, tokens_path=tokens_path,
        calendars=[{"id": "cal1", "label": "Cal One", "emoji": ""}],
        workflowy_event_ids=set(), skip_titles=[], lookahead_days=8,
        now_utc=datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc),
    )
    brain = read_brain(brain_path)
    assert [e["id"] for e in brain["events"]] == []


def test_tick_seeds_full_fetch_when_no_sync_token(tmp_path):
    """Missing sync token → bounded full fetch; brain gets the fetched
    events and a fresh syncToken is persisted."""
    brain_path = tmp_path / "brain.json"
    tokens_path = tmp_path / "tokens.json"
    _seed_brain(brain_path, [])
    _seed_tokens(tokens_path, {})  # no tokens at all

    svc = _FakeService({
        "FULL": {
            "items": [_raw_new_event("seeded_1", "2026-04-23T15:00:00-07:00")],
            "nextSyncToken": "fresh_v1",
        },
    })

    summary = _mod.run_tick(
        service=svc,
        brain_path=brain_path, tokens_path=tokens_path,
        calendars=[{"id": "cal1", "label": "Cal One", "emoji": ""}],
        workflowy_event_ids=set(), skip_titles=[], lookahead_days=8,
        now_utc=datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc),
    )
    assert summary["status"] == "ok"
    assert any("timeMin" in c for c in svc.calls), (
        "missing sync token should trigger a full fetch with timeMin/timeMax"
    )
    tokens = read_sync_tokens(tokens_path)
    assert tokens["cal1"]["sync_token"] == "fresh_v1"
    brain = read_brain(brain_path)
    assert [e["id"] for e in brain["events"]] == ["seeded_1"]


def test_tick_reseeds_on_410_gone(tmp_path):
    """Stale syncToken (410 GONE) → fall back to full fetch and reseed."""
    brain_path = tmp_path / "brain.json"
    tokens_path = tmp_path / "tokens.json"
    _seed_brain(brain_path, [])
    _seed_tokens(tokens_path, {
        "cal1": {"sync_token": "stale", "last_success_at": "seed", "last_error": None},
    })
    svc = _FakeService(
        {
            "FULL": {
                "items": [_raw_new_event("reseed_1", "2026-04-23T16:00:00-07:00")],
                "nextSyncToken": "fresh_v2",
            },
        },
        raise_410_for={"stale"},
    )
    summary = _mod.run_tick(
        service=svc,
        brain_path=brain_path, tokens_path=tokens_path,
        calendars=[{"id": "cal1", "label": "Cal One", "emoji": ""}],
        workflowy_event_ids=set(), skip_titles=[], lookahead_days=8,
        now_utc=datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc),
    )
    assert summary["status"] == "ok"
    tokens = read_sync_tokens(tokens_path)
    assert tokens["cal1"]["sync_token"] == "fresh_v2"


def test_tick_preserves_other_calendars_on_single_failure(tmp_path):
    """If cal1 fails, cal2 still advances. Failure is recorded in
    last_error but doesn't block the tick."""
    brain_path = tmp_path / "brain.json"
    tokens_path = tmp_path / "tokens.json"
    _seed_brain(brain_path, [])
    _seed_tokens(tokens_path, {
        "cal1": {"sync_token": "v1", "last_success_at": "seed", "last_error": None},
        "cal2": {"sync_token": "w1", "last_success_at": "seed", "last_error": None},
    })

    class _FlakyService(_FakeService):
        def list(self, **kwargs):
            self.calls.append(dict(kwargs))
            if kwargs.get("calendarId") == "cal1":
                return _ExplodingExec()
            return _Exec(kwargs, self.responses, self.raise_410_for)

    class _ExplodingExec:
        def execute(self):
            raise RuntimeError("transient failure on cal1")

    svc = _FlakyService({
        "w1": {
            "items": [_raw_new_event("cal2_evt", "2026-04-23T14:00:00-07:00")],
            "nextSyncToken": "w2",
        },
    })

    _mod.run_tick(
        service=svc,
        brain_path=brain_path, tokens_path=tokens_path,
        calendars=[
            {"id": "cal1", "label": "Cal One", "emoji": ""},
            {"id": "cal2", "label": "Cal Two", "emoji": ""},
        ],
        workflowy_event_ids=set(), skip_titles=[], lookahead_days=8,
        now_utc=datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc),
    )
    tokens = read_sync_tokens(tokens_path)
    # cal2 advanced
    assert tokens["cal2"]["sync_token"] == "w2"
    assert tokens["cal2"]["last_error"] is None
    # cal1 stayed at the stale token but recorded the error
    assert tokens["cal1"]["sync_token"] == "v1"
    assert "transient failure" in (tokens["cal1"]["last_error"] or "")
    # Brain still got cal2's event
    brain = read_brain(brain_path)
    assert [e["id"] for e in brain["events"]] == ["cal2_evt"]


def test_tick_marks_fetched_via_listener(tmp_path):
    """Brain's ``fetched_via`` flips from 'daily-build' to 'listener'
    after a successful tick — provenance for post-migration observability."""
    brain_path = tmp_path / "brain.json"
    tokens_path = tmp_path / "tokens.json"
    _seed_brain(brain_path, [])
    _seed_tokens(tokens_path, {
        "cal1": {"sync_token": "v1", "last_success_at": "seed", "last_error": None},
    })
    svc = _FakeService({"v1": {"items": [], "nextSyncToken": "v2"}})
    _mod.run_tick(
        service=svc,
        brain_path=brain_path, tokens_path=tokens_path,
        calendars=[{"id": "cal1", "label": "Cal One", "emoji": ""}],
        workflowy_event_ids=set(), skip_titles=[], lookahead_days=8,
        now_utc=datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc),
    )
    brain = read_brain(brain_path)
    assert brain["fetched_via"] == "listener"
