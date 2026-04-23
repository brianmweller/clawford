"""Tests for agents/shared/scripts/calendar-brain-build.py.

The daily full rebuild script. Exercises the pure-logic helpers
(``build_brain_payload``, ``build_legacy_index``) with fixture event
data; the OAuth + subprocess surface is out of scope for unit tests.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "calendar-brain-build.py"
)
_spec = importlib.util.spec_from_file_location("calendar_brain_build", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _raw_meeting():
    return {
        "id": "evt_jim",
        "summary": "1:1 with Jim",
        "start": {"dateTime": "2026-04-23T14:00:00-07:00"},
        "end": {"dateTime": "2026-04-23T14:30:00-07:00"},
        "hangoutLink": "https://meet.google.com/jim",
        "status": "confirmed",
    }


def _raw_event():
    return {
        "id": "evt_birthday",
        "summary": "Sofia's birthday",
        "start": {"date": "2026-04-26"},
        "end": {"date": "2026-04-27"},
        "status": "confirmed",
    }


def _raw_cancelled():
    return {
        "id": "evt_dead",
        "summary": "cancelled meeting",
        "start": {"dateTime": "2026-04-23T10:00:00-07:00"},
        "end": {"dateTime": "2026-04-23T10:30:00-07:00"},
        "hangoutLink": "https://meet.google.com/dead",
        "status": "cancelled",
    }


def test_build_brain_payload_produces_full_event_records():
    """The brain carries full normalized records (attendees, desc,
    conference_link, etc.) — not just the classification flags."""
    raw_by_cal = {
        "cal_brian": {
            "label": "the operator",
            "emoji": "",
            "events": [_raw_meeting()],
            "sync_token": "sync_brian_v1",
        },
    }
    now = datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc)
    payload, tokens = _mod.build_brain_payload(
        raw_by_cal=raw_by_cal,
        workflowy_event_ids=set(),
        skip_titles=[],
        time_min="2026-04-23T00:00:00-07:00",
        time_max="2026-05-01T00:00:00-07:00",
        now_utc=now,
    )
    assert payload["fetched_via"] == "daily-build"
    assert payload["generated_at"] == now.isoformat()
    assert len(payload["events"]) == 1
    evt = payload["events"][0]
    assert evt["id"] == "evt_jim"
    assert evt["conference_link"] == "https://meet.google.com/jim"
    assert evt["is_meeting"] is True
    assert evt["owner"] == "sergeant-murphy"
    assert evt["calendar_label"] == "the operator"

    # Tokens dict carries the per-calendar sync token + success stamp.
    assert tokens["cal_brian"]["sync_token"] == "sync_brian_v1"
    assert tokens["cal_brian"]["last_error"] is None


def test_build_brain_payload_drops_cancelled_events():
    raw_by_cal = {
        "cal_brian": {
            "label": "the operator", "emoji": "",
            "events": [_raw_meeting(), _raw_cancelled()],
            "sync_token": "v1",
        },
    }
    payload, _ = _mod.build_brain_payload(
        raw_by_cal=raw_by_cal,
        workflowy_event_ids=set(),
        skip_titles=[],
        time_min="", time_max="",
        now_utc=datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    ids = {e["id"] for e in payload["events"]}
    assert "evt_jim" in ids
    assert "evt_dead" not in ids


def test_build_brain_payload_applies_skip_titles():
    raw_by_cal = {
        "cal_brian": {
            "label": "the operator", "emoji": "",
            "events": [
                {
                    "id": "focus",
                    "summary": "Focus Time",
                    "start": {"dateTime": "2026-04-23T09:00:00-07:00"},
                    "end": {"dateTime": "2026-04-23T11:00:00-07:00"},
                    "hangoutLink": "https://meet.google.com/x",
                    "status": "confirmed",
                },
            ],
            "sync_token": "v1",
        },
    }
    payload, _ = _mod.build_brain_payload(
        raw_by_cal=raw_by_cal,
        workflowy_event_ids=set(),
        skip_titles=["Focus Time"],
        time_min="", time_max="",
        now_utc=datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    evt = payload["events"][0]
    # It still has a video link (is_meeting=True) so brain stores it —
    # but is_real_meeting is False so Murphy hides it.
    assert evt["is_meeting"] is True
    assert evt["is_real_meeting"] is False


def test_build_brain_payload_dedupes_across_calendars():
    """Same event appearing on two calendars (invite propagation) —
    keep the copy that saw a video link if one copy is stripped."""
    raw_by_cal = {
        "cal_full": {
            "label": "Full", "emoji": "",
            "events": [_raw_meeting()],
            "sync_token": "v1",
        },
        "cal_stripped": {
            "label": "Stripped", "emoji": "",
            "events": [
                {
                    "id": "evt_jim",
                    "summary": "1:1 with Jim",
                    "start": {"dateTime": "2026-04-23T14:00:00-07:00"},
                    "end": {"dateTime": "2026-04-23T14:30:00-07:00"},
                    "status": "confirmed",  # no hangoutLink
                },
            ],
            "sync_token": "v2",
        },
    }
    payload, _ = _mod.build_brain_payload(
        raw_by_cal=raw_by_cal,
        workflowy_event_ids=set(),
        skip_titles=[],
        time_min="", time_max="",
        now_utc=datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    # Only one record, and it's the one with the video link.
    records = [e for e in payload["events"] if e["id"] == "evt_jim"]
    assert len(records) == 1
    assert records[0]["is_meeting"] is True
    assert records[0]["calendar_label"] == "Full"


def test_build_brain_payload_records_fetch_error():
    """A calendar that failed to fetch shows up in tokens with an error
    message and None sync_token — the listener will re-seed it on the
    next tick."""
    raw_by_cal = {
        "cal_broken": {
            "label": "Broken", "emoji": "",
            "error": "HttpError 401 Unauthorized",
        },
    }
    payload, tokens = _mod.build_brain_payload(
        raw_by_cal=raw_by_cal,
        workflowy_event_ids=set(),
        skip_titles=[],
        time_min="", time_max="",
        now_utc=datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    assert payload["events"] == []
    assert tokens["cal_broken"]["sync_token"] is None
    assert "401" in tokens["cal_broken"]["last_error"]


def test_build_legacy_index_shape():
    """calendar-index.json needs the old thin {id: record} shape for
    backward compat. Derived from the brain payload, not re-fetched."""
    raw_by_cal = {
        "cal_brian": {
            "label": "the operator", "emoji": "",
            "events": [_raw_meeting(), _raw_event()],
            "sync_token": "v1",
        },
    }
    now = datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc)
    brain, _ = _mod.build_brain_payload(
        raw_by_cal=raw_by_cal,
        workflowy_event_ids=set(),
        skip_titles=[],
        time_min="2026-04-23T00:00:00-07:00",
        time_max="2026-05-01T00:00:00-07:00",
        now_utc=now,
    )
    legacy = _mod.build_legacy_index(brain)
    # Legacy shape: generated_at + window + events as {id: record} dict.
    assert isinstance(legacy["events"], dict)
    assert set(legacy["events"].keys()) == {"evt_jim", "evt_birthday"}
    assert legacy["events"]["evt_jim"]["owner"] == "sergeant-murphy"
    assert legacy["events"]["evt_birthday"]["owner"] == "mistress-mouse"
    assert legacy["event_count"] == 2
    assert legacy["generated_at"] == now.isoformat()
