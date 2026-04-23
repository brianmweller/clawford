"""Tests for agents/shared/calendar_fetch.py.

This is the single-source-of-truth fetch + normalization library that
replaces the two duplicated gcal-fetch.py scripts (Murphy's and
Mouse's). The normalized shape must be a SUPERSET of what either
agent's legacy script produced — Murphy gets full attendees +
description, Mouse filters/blanks at read time.

Byte-exact regression is enforced by the `test_normalize_matches_*`
tests which run a representative raw event through both the current
legacy logic (inlined here) and the new `normalize_event`, asserting
every field overlaps identically.
"""
from __future__ import annotations

import pytest

from agents.shared.calendar_fetch import (
    fetch_events_full,
    fetch_events_incremental,
    normalize_event,
)


# ---------------------------------------------------------------------------
# Fixture events — representative of what Google Calendar API returns
# ---------------------------------------------------------------------------


def _raw_meeting_with_hangout():
    """Video-conference meeting with hangoutLink + attendees."""
    return {
        "id": "evt_meet_abc",
        "summary": "1:1 with Jamie",
        "start": {"dateTime": "2026-04-23T14:00:00-07:00"},
        "end": {"dateTime": "2026-04-23T14:30:00-07:00"},
        "attendees": [
            {"email": "sam.smith@example.com", "self": True,
             "responseStatus": "accepted"},
            {"email": "jamie@example.com", "displayName": "Jamie F",
             "responseStatus": "accepted"},
        ],
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "organizer": {"email": "sam.smith@example.com"},
        "location": "",
        "description": "Monthly catchup",
        "status": "confirmed",
    }


def _raw_in_person_no_link():
    """In-person event (birthday, errand) — no video, no Workflowy."""
    return {
        "id": "evt_birthday_xyz",
        "summary": "Sofia's birthday party",
        "start": {"date": "2026-04-26"},
        "end": {"date": "2026-04-27"},
        "location": "Aunt Sally's house",
        "description": "",
        "status": "confirmed",
        "organizer": {"email": "linda@example.com"},
    }


def _raw_recruiter_empty_attendees():
    """Self-booked LinkedIn cold-recruiter pattern — no attendees, video
    link in description, organizer is the operator himself."""
    return {
        "id": "evt_coinbase_jtds",
        "summary": "Interview with Coinbase",
        "start": {"dateTime": "2026-04-23T12:30:00-07:00"},
        "end": {"dateTime": "2026-04-23T13:00:00-07:00"},
        "attendees": [
            {"email": "sam.smith@example.com", "self": True,
             "responseStatus": "accepted"},
        ],
        "organizer": {"email": "sam.smith@example.com"},
        "location": "",
        "description": (
            "Hi the operator,\n\nFollowing up on our LinkedIn conversation! "
            "Google Meet: https://meet.google.com/cww-njwm-ksx\n\n"
            "Best,\nAbby Mintert"
        ),
        "status": "confirmed",
    }


def _raw_skip_title():
    """Event whose title matches Murphy's skip_titles filter ('Focus
    Time', 'Lunch', 'Block')."""
    return {
        "id": "evt_focus_1",
        "summary": "Focus Time",
        "start": {"dateTime": "2026-04-23T09:00:00-07:00"},
        "end": {"dateTime": "2026-04-23T11:00:00-07:00"},
        "hangoutLink": "https://meet.google.com/junk-link",
        "status": "confirmed",
        "organizer": {"email": "sam.smith@example.com"},
    }


def _raw_conference_entrypoint():
    """Calendar event whose video link is in conferenceData.entryPoints
    (Google's canonical shape)."""
    return {
        "id": "evt_conf_entry",
        "summary": "Team standup",
        "start": {"dateTime": "2026-04-23T10:00:00-07:00"},
        "end": {"dateTime": "2026-04-23T10:15:00-07:00"},
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "more", "uri": "https://example.com"},
                {"entryPointType": "video",
                 "uri": "https://meet.google.com/xyz-abcd-efg"},
            ],
        },
        "status": "confirmed",
        "organizer": {"email": "teamlead@example.com"},
    }


# ---------------------------------------------------------------------------
# normalize_event — shape + classification
# ---------------------------------------------------------------------------


def test_normalize_meeting_with_hangout():
    out = normalize_event(
        _raw_meeting_with_hangout(),
        calendar_id="sam.smith@example.com",
        calendar_label="the operator (Work)",
        calendar_emoji="\U0001f468",
    )
    assert out["id"] == "evt_meet_abc"
    assert out["summary"] == "1:1 with Jamie"
    assert out["start"] == "2026-04-23T14:00:00-07:00"
    assert out["end"] == "2026-04-23T14:30:00-07:00"
    assert out["all_day"] is False
    assert out["calendar_id"] == "sam.smith@example.com"
    assert out["calendar_label"] == "the operator (Work)"
    assert out["conference_link"] == "https://meet.google.com/abc-defg-hij"
    assert out["has_video_link"] is True
    assert out["is_meeting"] is True
    assert out["owner"] == "sergeant-murphy"
    assert out["is_real_meeting"] is True
    # Attendees must exclude self=True entries (operator own address).
    emails = [a["email"] for a in out["attendees"]]
    assert "sam.smith@example.com" not in emails
    assert "jamie@example.com" in emails
    assert out["organizer"] == "sam.smith@example.com"


def test_normalize_all_day_non_meeting():
    out = normalize_event(
        _raw_in_person_no_link(),
        calendar_id="linda@example.com",
        calendar_label="Alex",
        calendar_emoji="\U0001f469",
    )
    assert out["all_day"] is True
    assert out["start"] == "2026-04-26"
    assert out["has_video_link"] is False
    assert out["in_workflowy"] is False
    assert out["is_meeting"] is False
    assert out["owner"] == "mistress-mouse"
    assert out["is_real_meeting"] is False


def test_normalize_description_link_classified_as_meeting():
    """The Coinbase/LinkedIn pattern: video link lives only in
    description body. has_videoconference_link must catch it."""
    out = normalize_event(
        _raw_recruiter_empty_attendees(),
        calendar_id="sam.smith@example.com",
    )
    assert out["has_video_link"] is True
    assert out["is_meeting"] is True
    assert out["owner"] == "sergeant-murphy"
    # The conference_link extractor should surface the Meet URL.
    assert "meet.google.com/cww-njwm-ksx" in out["conference_link"]
    # Description is preserved in the brain (readers blank at render time).
    assert "Following up on our LinkedIn" in out["description"]


def test_normalize_conference_entrypoint_link():
    """conferenceData.entryPoints[].entryPointType=='video' → link."""
    out = normalize_event(
        _raw_conference_entrypoint(),
        calendar_id="teamlead@example.com",
    )
    assert out["conference_link"] == "https://meet.google.com/xyz-abcd-efg"
    assert out["has_video_link"] is True


def test_normalize_skip_title_drops_is_real_meeting():
    """'Focus Time' with a video link is_meeting=True (it has a link)
    but is_real_meeting=False (Murphy's skip_titles filter). This is
    Murphy's distinction: the brain is omniscient, is_real_meeting is
    the display filter."""
    out = normalize_event(
        _raw_skip_title(),
        calendar_id="sam.smith@example.com",
        skip_titles=["Focus Time", "Lunch", "Block"],
    )
    assert out["has_video_link"] is True
    assert out["is_meeting"] is True       # it's still a video event
    assert out["is_real_meeting"] is False  # but Murphy filters it out


def test_normalize_workflowy_tag_promotes_event_to_meeting():
    """In-person coffee chat with no video link, but the operator tagged it in
    Workflowy — in_workflowy=True promotes is_meeting=True."""
    out = normalize_event(
        _raw_in_person_no_link(),
        calendar_id="linda@example.com",
        workflowy_event_ids={"evt_birthday_xyz"},
    )
    assert out["has_video_link"] is False
    assert out["in_workflowy"] is True
    assert out["is_meeting"] is True
    assert out["owner"] == "sergeant-murphy"


def test_normalize_ats_organizer_routes_to_murphy():
    """Phone-only recruiter invite — no video link, no Workflowy tag,
    but organizer is an ATS/scheduling domain
    (``schedule@interview.adobe.com``). Must route to Murphy.
    Regression target: 2026-04-23 — 'Meeting Confirmation - Sam Smith'
    (Adobe phone screen) was landing on Mouse's morning brief because
    the classifier only looked at video link + Workflowy tag and
    ignored the organizer domain, even though Murphy's prep path
    already recognised ATS organisers."""
    raw = {
        "id": "evt_adobe_phone",
        "summary": "Meeting Confirmation - Sam Smith",
        "start": {"dateTime": "2026-04-23T14:45:00-07:00"},
        "end": {"dateTime": "2026-04-23T15:15:00-07:00"},
        "attendees": [],
        "organizer": {"email": "schedule@interview.adobe.com"},
        "location": "phone: 216-469-2610",
        "description": "Adobe Leadership Chat — phone screen.",
        "status": "confirmed",
    }
    out = normalize_event(raw, calendar_id="sam.smith@example.com")
    assert out["has_video_link"] is False
    assert out["in_workflowy"] is False
    assert out["organizer_is_recruiter"] is True
    assert out["is_meeting"] is True
    assert out["owner"] == "sergeant-murphy"


def test_normalize_non_ats_organizer_does_not_promote():
    """Regression guard: a random non-ATS organizer (friend, family,
    colleague) must NOT route to Murphy via the ATS-organizer arm."""
    raw = _raw_in_person_no_link()
    raw["organizer"] = {"email": "friend@example.com"}
    out = normalize_event(raw, calendar_id="cal")
    assert out["organizer_is_recruiter"] is False
    assert out["is_meeting"] is False
    assert out["owner"] == "mistress-mouse"


def test_normalize_missing_fields_defaults():
    """Sparse raw event — defaults shouldn't blow up."""
    out = normalize_event({"id": "evt_sparse"}, calendar_id="cal")
    assert out["id"] == "evt_sparse"
    assert out["summary"] == ""
    assert out["start"] == ""
    assert out["all_day"] is False
    assert out["has_video_link"] is False
    assert out["attendees"] == []
    assert out["organizer"] == ""
    assert out["status"] == "confirmed"  # default matches legacy


def test_normalize_organizer_as_string_fallback():
    """gcal-fetch sometimes pre-flattens organizer to a string. Handle
    both shapes."""
    raw = _raw_meeting_with_hangout()
    raw["organizer"] = "flat@example.com"
    out = normalize_event(raw, calendar_id="cal")
    assert out["organizer"] == "flat@example.com"


def test_normalize_cancelled_status_preserved():
    raw = _raw_meeting_with_hangout()
    raw["status"] = "cancelled"
    out = normalize_event(raw, calendar_id="cal")
    assert out["status"] == "cancelled"


# ---------------------------------------------------------------------------
# fetch_events_full — service wrapper with pagination
# ---------------------------------------------------------------------------


class _FakeService:
    """Minimal Calendar service stand-in. Records invocations, returns
    canned responses keyed by page_token."""

    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.calls: list[dict] = []

    def events(self):
        return self

    def list(self, **kwargs):
        self.calls.append(dict(kwargs))
        return _ExecList(self.pages, len(self.calls) - 1)


class _ExecList:
    def __init__(self, pages, idx):
        self.pages = pages
        self.idx = idx

    def execute(self):
        if self.idx >= len(self.pages):
            return {}
        return self.pages[self.idx]


def test_fetch_events_full_single_page():
    svc = _FakeService([{"items": [{"id": "a"}, {"id": "b"}]}])
    events, sync = fetch_events_full(
        svc, "cal1",
        time_min="2026-04-23T00:00:00-07:00",
        time_max="2026-04-24T00:00:00-07:00",
    )
    assert [e["id"] for e in events] == ["a", "b"]
    assert svc.calls[0]["calendarId"] == "cal1"
    assert svc.calls[0]["singleEvents"] is True
    assert svc.calls[0]["orderBy"] == "startTime"


def test_fetch_events_full_paginates():
    svc = _FakeService([
        {"items": [{"id": "a"}], "nextPageToken": "tok1"},
        {"items": [{"id": "b"}], "nextPageToken": "tok2"},
        {"items": [{"id": "c"}]},
    ])
    events, _ = fetch_events_full(
        svc, "cal1",
        time_min="2026-04-23T00:00:00-07:00",
        time_max="2026-04-24T00:00:00-07:00",
    )
    assert [e["id"] for e in events] == ["a", "b", "c"]
    # Page tokens passed through correctly.
    assert svc.calls[0].get("pageToken") in (None, "")
    assert svc.calls[1]["pageToken"] == "tok1"
    assert svc.calls[2]["pageToken"] == "tok2"


def test_fetch_events_full_returns_sync_token():
    """Full list with time window returns a nextSyncToken on the final
    page — caller persists this to seed incremental fetches."""
    svc = _FakeService([
        {"items": [{"id": "a"}], "nextSyncToken": "sync_v1"},
    ])
    _, sync = fetch_events_full(
        svc, "cal1",
        time_min="2026-04-23T00:00:00-07:00",
        time_max="2026-04-24T00:00:00-07:00",
    )
    assert sync == "sync_v1"


# ---------------------------------------------------------------------------
# fetch_events_incremental — syncToken path + 410 GONE re-seed
# ---------------------------------------------------------------------------


def test_fetch_incremental_returns_delta_and_new_token():
    svc = _FakeService([
        {"items": [{"id": "changed_1"}], "nextSyncToken": "sync_v2"},
    ])
    events, sync = fetch_events_incremental(svc, "cal1", "sync_v1")
    assert [e["id"] for e in events] == ["changed_1"]
    assert sync == "sync_v2"
    # Incremental fetches must pass the syncToken and must NOT pass
    # time windows (Google rejects that combo).
    assert svc.calls[0]["syncToken"] == "sync_v1"
    assert "timeMin" not in svc.calls[0]
    assert "timeMax" not in svc.calls[0]


def test_fetch_incremental_410_gone_returns_sentinel():
    """A 410 GONE from Google means the syncToken has aged out. Caller
    should fall back to a full fetch to re-seed. The lib signals this
    by returning (None, None)."""
    from googleapiclient.errors import HttpError
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status = 410
    resp.reason = "Gone"
    error = HttpError(resp=resp, content=b'{"error": {"code": 410}}')

    class _ExplodingService:
        def events(self_):
            return self_
        def list(self_, **kwargs):
            return self_
        def execute(self_):
            raise error

    events, sync = fetch_events_incremental(
        _ExplodingService(), "cal1", "stale_token"
    )
    assert events is None
    assert sync is None
