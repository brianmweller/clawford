"""Tests for the freebusy request/response helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agents.shared.gcal_freebusy import (
    build_freebusy_request_body,
    parse_freebusy_response,
    query_busy,
)


# --- build_freebusy_request_body ---

def test_body_uses_utc_z_suffix():
    pt = ZoneInfo("America/Los_Angeles")
    start = datetime(2026, 4, 22, 9, 0, tzinfo=pt)
    end = datetime(2026, 4, 22, 18, 0, tzinfo=pt)
    body = build_freebusy_request_body(start, end)
    assert body["timeMin"].endswith("Z")
    assert body["timeMax"].endswith("Z")
    # 09:00 PT on 2026-04-22 (UTC-7, DST) → 16:00 UTC
    assert body["timeMin"] == "2026-04-22T16:00:00Z"


def test_body_defaults_to_primary_calendar():
    now = datetime.now(timezone.utc)
    body = build_freebusy_request_body(now, now)
    assert body["items"] == [{"id": "primary"}]


def test_body_supports_multiple_calendars():
    now = datetime.now(timezone.utc)
    body = build_freebusy_request_body(now, now, ["primary", "work@example.com"])
    assert body["items"] == [{"id": "primary"}, {"id": "work@example.com"}]


def test_body_rejects_naive_datetimes():
    naive = datetime(2026, 4, 22, 9, 0)
    with pytest.raises(ValueError):
        build_freebusy_request_body(naive, naive)


# --- parse_freebusy_response ---

def test_parse_extracts_busy_from_single_calendar():
    resp = {
        "calendars": {
            "primary": {
                "busy": [
                    {"start": "2026-04-22T16:00:00Z", "end": "2026-04-22T17:00:00Z"},
                    {"start": "2026-04-22T19:00:00Z", "end": "2026-04-22T20:00:00Z"},
                ]
            }
        }
    }
    out = parse_freebusy_response(resp)
    assert len(out) == 2
    assert out[0]["start"] == "2026-04-22T16:00:00Z"


def test_parse_merges_overlapping_blocks_across_calendars():
    resp = {
        "calendars": {
            "primary": {"busy": [
                {"start": "2026-04-22T16:00:00Z", "end": "2026-04-22T17:00:00Z"},
            ]},
            "work": {"busy": [
                {"start": "2026-04-22T16:30:00Z", "end": "2026-04-22T18:00:00Z"},
            ]},
        }
    }
    out = parse_freebusy_response(resp)
    # 16:00-17:00 and 16:30-18:00 overlap → merged to 16:00-18:00
    assert out == [{"start": "2026-04-22T16:00:00Z", "end": "2026-04-22T18:00:00Z"}]


def test_parse_sorts_disjoint_blocks():
    resp = {
        "calendars": {
            "c1": {"busy": [
                {"start": "2026-04-23T10:00:00Z", "end": "2026-04-23T11:00:00Z"},
            ]},
            "c2": {"busy": [
                {"start": "2026-04-22T16:00:00Z", "end": "2026-04-22T17:00:00Z"},
            ]},
        }
    }
    out = parse_freebusy_response(resp)
    assert len(out) == 2
    assert out[0]["start"] < out[1]["start"]


def test_parse_empty_response_returns_empty_list():
    assert parse_freebusy_response({}) == []
    assert parse_freebusy_response({"calendars": {}}) == []
    assert parse_freebusy_response({"calendars": {"primary": {"busy": []}}}) == []


def test_parse_skips_malformed_blocks():
    resp = {
        "calendars": {
            "primary": {"busy": [
                {"start": "2026-04-22T16:00:00Z"},        # missing end
                {"end": "2026-04-22T17:00:00Z"},          # missing start
                {"start": "2026-04-22T18:00:00Z", "end": "2026-04-22T19:00:00Z"},
            ]}
        }
    }
    out = parse_freebusy_response(resp)
    assert len(out) == 1


# --- query_busy (error resilience) ---

class _FailingService:
    def freebusy(self):
        raise RuntimeError("calendar API down")


def test_query_busy_returns_empty_list_on_error():
    now = datetime.now(timezone.utc)
    out = query_busy(_FailingService(), now, now)
    assert out == []


class _FakeService:
    def __init__(self, resp):
        self._resp = resp

    def freebusy(self):
        class _Q:
            def __init__(self, resp):
                self._resp = resp

            def query(self, body):
                self._body = body

                class _Exec:
                    def __init__(self, resp):
                        self._resp = resp

                    def execute(self_inner):
                        return self._resp

                return _Exec(self._resp)

        return _Q(self._resp)


def test_query_busy_happy_path():
    resp = {"calendars": {"primary": {"busy": [
        {"start": "2026-04-22T16:00:00Z", "end": "2026-04-22T17:00:00Z"},
    ]}}}
    pt = ZoneInfo("America/Los_Angeles")
    out = query_busy(
        _FakeService(resp),
        datetime(2026, 4, 22, 9, 0, tzinfo=pt),
        datetime(2026, 4, 22, 18, 0, tzinfo=pt),
    )
    assert out == [{"start": "2026-04-22T16:00:00Z", "end": "2026-04-22T17:00:00Z"}]
