"""Shared calendar-index classifier + reader.

Introduced 2026-04-18 to give Sergeant Murphy and Mistress Mouse a
single source of truth for "is this calendar item a meeting?" Before
the index, each agent ran its own fetch and its own classifier —
Mouse deliberately blanked each event's description before classifying,
so it could never see a Webex / Zoom / Meet link pasted into the
description. The brain index is built once per tick on the raw
Google Calendar events and both agents read it.

Schema at ~/Dropbox/openclaw-backup/status/calendar-index.json:

    {
      "generated_at": "...",
      "window": {"start": "...", "end": "..."},
      "events": {
        "<event_id>": {
          "summary": "...",
          "start": "...",
          "end": "...",
          "calendar_id": "...",
          "has_video_link": true,
          "is_meeting": true,
          "owner": "sergeant-murphy" | "mistress-mouse"
        }
      }
    }
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.shared.calendar_index import (
    classify_event,
    load_brain_index,
    meeting_event_ids,
)


def test_classify_event_with_hangout_link_is_meeting():
    raw = {"id": "e1", "summary": "Sync", "hangoutLink": "https://meet.google.com/abc"}
    r = classify_event(raw, calendar_id="operator@gmail.com")
    assert r["is_meeting"] is True
    assert r["has_video_link"] is True
    assert r["owner"] == "sergeant-murphy"
    assert r["id"] == "e1"
    assert r["summary"] == "Sync"
    assert r["calendar_id"] == "operator@gmail.com"


def test_classify_event_with_webex_in_description_is_meeting():
    raw = {
        "id": "e2",
        "summary": "Fairport Check In",
        "description": "Join via https://hightower.webex.com/hightower/j.php?MTID=m329",
    }
    r = classify_event(raw, calendar_id="operator@gmail.com")
    assert r["is_meeting"] is True
    assert r["owner"] == "sergeant-murphy"


def test_classify_event_plain_in_person_is_event():
    raw = {
        "id": "e3",
        "summary": "Exploring Ballet",
        "description": "meet at the studio",
        "attendees": [{"email": "sam@example.com"}],
    }
    r = classify_event(raw, calendar_id="operator@gmail.com")
    assert r["is_meeting"] is False
    assert r["owner"] == "mistress-mouse"


def test_classify_event_workflowy_override_promotes_in_person_item():
    """the operator's human-in-the-loop override: if he's tagged the event in
    Workflowy (e.g. an in-person coffee worth coaching on) it's
    Murphy's, even with no video link. Physical signal + human signal
    OR — either one flips is_meeting to True."""
    raw = {
        "id": "coffee-123",
        "summary": "Coffee with Dan",
        "attendees": [{"email": "dan@x.com"}],
    }
    r = classify_event(
        raw,
        calendar_id="operator@gmail.com",
        workflowy_event_ids={"coffee-123"},
    )
    assert r["in_workflowy"] is True
    assert r["is_meeting"] is True
    assert r["owner"] == "sergeant-murphy"


def test_classify_event_workflowy_absent_leaves_plain_event_to_mouse():
    raw = {"id": "ballet-9", "summary": "Exploring Ballet"}
    r = classify_event(
        raw,
        calendar_id="operator@gmail.com",
        workflowy_event_ids={"other-event-id"},
    )
    assert r["in_workflowy"] is False
    assert r["is_meeting"] is False


def test_classify_event_video_link_wins_without_needing_workflowy_set():
    raw = {"id": "v1", "summary": "Sync", "hangoutLink": "https://meet.google.com/x"}
    r = classify_event(raw, calendar_id="operator@gmail.com")
    assert r["in_workflowy"] is False
    assert r["is_meeting"] is True


def test_classify_normalizes_start_end_date_time_and_all_day():
    raw_dt = {"id": "e4", "summary": "Call",
              "hangoutLink": "https://meet.google.com/x",
              "start": {"dateTime": "2026-04-18T10:00:00-07:00"},
              "end": {"dateTime": "2026-04-18T10:30:00-07:00"}}
    r_dt = classify_event(raw_dt, calendar_id="operator@gmail.com")
    assert r_dt["start"] == "2026-04-18T10:00:00-07:00"
    assert r_dt["end"] == "2026-04-18T10:30:00-07:00"
    assert r_dt["all_day"] is False

    raw_day = {"id": "e5", "summary": "Holiday",
               "start": {"date": "2026-04-19"}, "end": {"date": "2026-04-20"}}
    r_day = classify_event(raw_day, calendar_id="operator@gmail.com")
    assert r_day["start"] == "2026-04-19"
    assert r_day["all_day"] is True


def test_classify_preserves_event_id_even_when_summary_missing():
    raw = {"id": "e6"}
    r = classify_event(raw, calendar_id="operator@gmail.com")
    assert r["id"] == "e6"
    assert r["summary"] == ""


def test_load_brain_index_returns_events_dict(tmp_path):
    path = tmp_path / "calendar-index.json"
    path.write_text(json.dumps({
        "generated_at": "2026-04-18T10:25:00Z",
        "window": {"start": "2026-04-18", "end": "2026-04-25"},
        "events": {
            "e1": {"summary": "Sync", "is_meeting": True, "owner": "sergeant-murphy"},
            "e2": {"summary": "Ballet", "is_meeting": False, "owner": "mistress-mouse"},
        },
    }), encoding="utf-8")
    idx = load_brain_index(path)
    assert set(idx.keys()) == {"e1", "e2"}
    assert idx["e1"]["is_meeting"] is True


def test_load_brain_index_returns_empty_dict_when_file_missing(tmp_path):
    assert load_brain_index(tmp_path / "nope.json") == {}


def test_load_brain_index_returns_empty_on_unreadable_json(tmp_path):
    path = tmp_path / "calendar-index.json"
    path.write_text("not json {{")
    assert load_brain_index(path) == {}


def test_meeting_event_ids_pulls_ids_where_is_meeting_true(tmp_path):
    path = tmp_path / "calendar-index.json"
    path.write_text(json.dumps({
        "events": {
            "a": {"is_meeting": True},
            "b": {"is_meeting": False},
            "c": {"is_meeting": True},
            "d": {},
        }
    }), encoding="utf-8")
    ids = meeting_event_ids(path)
    assert ids == {"a", "c"}


def test_meeting_event_ids_missing_file_is_empty_set(tmp_path):
    assert meeting_event_ids(tmp_path / "absent.json") == set()
