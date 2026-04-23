"""Tests for agents/shared/calendar_brain.py.

The brain is the single-source-of-truth cache that both Murphy and
Mouse read from (never write). Structure:

  ~/.clawford/calendar-brain/calendar-brain.json   # events + metadata
  ~/.clawford/calendar-brain/calendar-brain-tokens.json  # per-cal syncToken

This test file covers the pure read/write helpers. Integration with
the listener + daily-build scripts lives in their own test files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.shared.calendar_brain import (
    atomic_write_brain,
    read_brain,
    read_sync_tokens,
    write_sync_tokens,
)


def _evt(eid, start, owner="sergeant-murphy", summary="Test", is_real=True):
    """Minimal normalized event for tests — matches calendar_fetch's
    output shape."""
    return {
        "id": eid,
        "calendar_id": "cal1",
        "calendar_label": "Test Cal",
        "calendar_emoji": "",
        "summary": summary,
        "start": start,
        "end": start,
        "all_day": False,
        "location": "",
        "description": "",
        "status": "confirmed",
        "attendees": [],
        "organizer": "",
        "conference_link": "",
        "has_video_link": owner == "sergeant-murphy",
        "in_workflowy": False,
        "is_meeting": owner == "sergeant-murphy",
        "owner": owner,
        "is_real_meeting": is_real and owner == "sergeant-murphy",
    }


# ---------------------------------------------------------------------------
# atomic_write_brain / read_brain round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_round_trips_full_payload(tmp_path):
    path = tmp_path / "calendar-brain.json"
    events = [
        _evt("a", "2026-04-23T14:00:00-07:00"),
        _evt("b", "2026-04-23T15:00:00-07:00", owner="mistress-mouse"),
    ]
    atomic_write_brain(path, {
        "generated_at": "2026-04-23T01:00:00+00:00",
        "fetched_via": "daily-build",
        "window": {"time_min": "2026-04-23T00:00:00-07:00",
                   "time_max": "2026-05-01T00:00:00-07:00"},
        "events": events,
    })

    payload = read_brain(path)
    assert payload["fetched_via"] == "daily-build"
    assert len(payload["events"]) == 2


def test_read_missing_file_returns_empty_payload(tmp_path):
    """Degrade-open: missing brain file yields a well-formed empty
    payload so readers don't need to special-case the first-run state."""
    result = read_brain(tmp_path / "does-not-exist.json")
    assert result == {"events": [], "generated_at": None,
                      "fetched_via": None, "window": None}


def test_read_malformed_json_returns_empty_payload(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json at all {", encoding="utf-8")
    result = read_brain(path)
    assert result["events"] == []


def test_read_brain_filters_by_owner(tmp_path):
    path = tmp_path / "brain.json"
    atomic_write_brain(path, {
        "events": [
            _evt("murphy_evt", "2026-04-23T14:00:00-07:00"),
            _evt("mouse_evt", "2026-04-23T15:00:00-07:00",
                 owner="mistress-mouse"),
        ],
    })
    murphy = read_brain(path, owner="sergeant-murphy")
    mouse = read_brain(path, owner="mistress-mouse")
    assert [e["id"] for e in murphy["events"]] == ["murphy_evt"]
    assert [e["id"] for e in mouse["events"]] == ["mouse_evt"]


def test_read_brain_filters_by_date_range(tmp_path):
    path = tmp_path / "brain.json"
    atomic_write_brain(path, {
        "events": [
            _evt("today", "2026-04-23T14:00:00-07:00"),
            _evt("tomorrow", "2026-04-24T09:00:00-07:00"),
            _evt("next_week", "2026-04-30T10:00:00-07:00"),
        ],
    })
    # date + days=1 yields today only
    r = read_brain(path, date="2026-04-23", days=1)
    assert [e["id"] for e in r["events"]] == ["today"]
    # date + days=2 yields today + tomorrow
    r = read_brain(path, date="2026-04-23", days=2)
    assert [e["id"] for e in r["events"]] == ["today", "tomorrow"]


def test_read_brain_owner_filter_applies_is_real_meeting_for_murphy(tmp_path):
    """When reading as Murphy, events with is_real_meeting=False are
    dropped — those are video events that hit the skip_titles filter
    (Focus Time, Lunch, Block). Mouse's view doesn't apply this filter."""
    path = tmp_path / "brain.json"
    atomic_write_brain(path, {
        "events": [
            _evt("jim_coffee", "2026-04-23T09:00:00-07:00",
                 summary="1:1 Jim", is_real=True),
            _evt("focus_time", "2026-04-23T10:00:00-07:00",
                 summary="Focus Time", is_real=False),
        ],
    })
    murphy = read_brain(path, owner="sergeant-murphy")
    assert [e["id"] for e in murphy["events"]] == ["jim_coffee"]


def test_atomic_write_uses_tmp_file(tmp_path):
    """Write must be atomic — a crash mid-write shouldn't corrupt the
    live brain. Verified by checking there's no intermediate file left
    behind and that the final file is valid JSON."""
    path = tmp_path / "brain.json"
    atomic_write_brain(path, {"events": [_evt("x", "2026-04-23")]})

    # No tmp file lingering
    assert not (tmp_path / "brain.json.tmp").exists()
    # File is valid JSON
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["events"][0]["id"] == "x"


def test_atomic_write_creates_parent_dir(tmp_path):
    """Parent directory may not exist on first run. Writer must mkdir
    -p so callers don't have to."""
    path = tmp_path / "nested" / "dir" / "brain.json"
    atomic_write_brain(path, {"events": []})
    assert path.exists()


# ---------------------------------------------------------------------------
# sync_tokens
# ---------------------------------------------------------------------------


def test_sync_tokens_round_trip(tmp_path):
    path = tmp_path / "tokens.json"
    tokens = {
        "sam.smith@example.com": {
            "sync_token": "abc123",
            "last_success_at": "2026-04-23T01:00:00+00:00",
            "last_error": None,
        },
        "linda@example.com": {
            "sync_token": "def456",
            "last_success_at": "2026-04-23T01:00:00+00:00",
            "last_error": None,
        },
    }
    write_sync_tokens(path, tokens)
    loaded = read_sync_tokens(path)
    assert loaded == tokens


def test_read_sync_tokens_missing_file_returns_empty(tmp_path):
    assert read_sync_tokens(tmp_path / "does-not-exist.json") == {}


def test_read_sync_tokens_malformed_returns_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("garbage", encoding="utf-8")
    assert read_sync_tokens(path) == {}


def test_write_sync_tokens_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "tokens.json"
    write_sync_tokens(path, {"cal1": {"sync_token": "v1"}})
    assert path.exists()


# ---------------------------------------------------------------------------
# read_brain_if_fresh — freshness gate for tools.py readers
# ---------------------------------------------------------------------------


from datetime import datetime, timezone

from agents.shared.calendar_brain import read_brain_if_fresh


def _write_brain_with_time(path, generated_at, events):
    atomic_write_brain(path, {
        "generated_at": generated_at,
        "fetched_via": "listener",
        "window": None,
        "events": events,
    })


def test_read_brain_if_fresh_returns_payload_when_fresh(tmp_path):
    path = tmp_path / "brain.json"
    now = datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc)
    # Generated 30s ago — well within the 600s window
    _write_brain_with_time(path, "2026-04-23T00:59:30+00:00", [
        _evt("e1", "2026-04-23T14:00:00-07:00"),
    ])
    out = read_brain_if_fresh(path, now=now, disable_marker="")
    assert out is not None
    assert len(out["events"]) == 1


def test_read_brain_if_fresh_returns_none_when_stale(tmp_path):
    path = tmp_path / "brain.json"
    now = datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc)
    # Generated 20 min ago — past the 600s max_age default
    _write_brain_with_time(path, "2026-04-23T00:40:00+00:00", [
        _evt("e1", "2026-04-23T14:00:00-07:00"),
    ])
    out = read_brain_if_fresh(path, now=now, disable_marker="")
    assert out is None


def test_read_brain_if_fresh_returns_none_when_missing(tmp_path):
    out = read_brain_if_fresh(
        tmp_path / "does-not-exist.json",
        disable_marker="",
    )
    assert out is None


def test_read_brain_if_fresh_returns_none_when_disabled(tmp_path):
    path = tmp_path / "brain.json"
    marker = tmp_path / "disable-me"
    marker.write_text("", encoding="utf-8")
    now = datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc)
    _write_brain_with_time(path, "2026-04-23T00:59:59+00:00", [
        _evt("e1", "2026-04-23T14:00:00-07:00"),
    ])
    out = read_brain_if_fresh(
        path, now=now, disable_marker=str(marker),
    )
    assert out is None


def test_read_brain_if_fresh_owner_filter_applied(tmp_path):
    path = tmp_path / "brain.json"
    now = datetime(2026, 4, 23, 1, 0, tzinfo=timezone.utc)
    _write_brain_with_time(path, "2026-04-23T00:59:30+00:00", [
        _evt("murphy_e", "2026-04-23T14:00:00-07:00"),
        _evt("mouse_e", "2026-04-23T15:00:00-07:00", owner="mistress-mouse"),
    ])
    out = read_brain_if_fresh(
        path, owner="sergeant-murphy", now=now, disable_marker="",
    )
    assert [e["id"] for e in out["events"]] == ["murphy_e"]
