"""Tests for agents/shared/gmail_watch.py.

Pure-library tests. No Gmail API calls — the service argument is a fake
whose .users().watch() and .users().stop() are tracked via a small
Recorder.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
sys.path.insert(0, str(SHARED_DIR))

from gmail_watch import (  # type: ignore  # noqa: E402
    WatchState,
    create_watch,
    load_watch_state,
    save_watch_state,
    stop_watch,
)


class _FakeExecutable:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeUsers:
    def __init__(self):
        self.watch_calls: list[dict] = []
        self.stop_calls: list[dict] = []
        self.watch_response = {"historyId": "123", "expiration": "1700000000000"}
        self.stop_response = {}

    def watch(self, userId, body):  # noqa: N803 — matches Gmail API
        self.watch_calls.append({"userId": userId, "body": body})
        return _FakeExecutable(self.watch_response)

    def stop(self, userId):  # noqa: N803
        self.stop_calls.append({"userId": userId})
        return _FakeExecutable(self.stop_response)


class _FakeService:
    def __init__(self):
        self._users = _FakeUsers()

    def users(self):
        return self._users


# ---- create_watch -----------------------------------------------------


def test_create_watch_posts_inbox_label_and_topic():
    service = _FakeService()
    result = create_watch(
        service,
        topic_name="projects/foo/topics/gmail-huckle-inbox",
    )
    users = service.users()
    assert len(users.watch_calls) == 1
    call = users.watch_calls[0]
    assert call["userId"] == "me"
    assert call["body"]["topicName"] == "projects/foo/topics/gmail-huckle-inbox"
    assert call["body"]["labelIds"] == ["INBOX"]
    assert call["body"]["labelFilterAction"] == "include"
    assert result["historyId"] == "123"
    assert result["expiration"] == "1700000000000"


def test_create_watch_custom_label_ids():
    service = _FakeService()
    create_watch(
        service,
        topic_name="projects/foo/topics/t",
        label_ids=["INBOX", "IMPORTANT"],
    )
    assert service.users().watch_calls[0]["body"]["labelIds"] == ["INBOX", "IMPORTANT"]


# ---- stop_watch -------------------------------------------------------


def test_stop_watch_calls_users_stop():
    service = _FakeService()
    stop_watch(service)
    assert service.users().stop_calls == [{"userId": "me"}]


# ---- WatchState round-trip --------------------------------------------


def test_watch_state_save_and_load(tmp_path):
    path = tmp_path / "watch.json"
    state = WatchState(
        history_id="42",
        expiration_ms=1_700_000_000_000,
        topic="projects/foo/topics/t",
    )
    save_watch_state(path, state)

    loaded = load_watch_state(path)
    assert loaded == state


def test_load_watch_state_missing_returns_none(tmp_path):
    assert load_watch_state(tmp_path / "nope.json") is None


def test_load_watch_state_corrupt_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_watch_state(path) is None


def test_save_watch_state_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "deep" / "watch.json"
    state = WatchState(history_id="1", expiration_ms=1, topic="t")
    save_watch_state(path, state)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["history_id"] == "1"


# ---- expiration helpers -----------------------------------------------


def test_watch_state_is_expiring_within():
    # expiration 1 hour from now
    now_ms = 1_700_000_000_000
    state = WatchState(
        history_id="1",
        expiration_ms=now_ms + 3600_000,
        topic="t",
    )
    # expires within 2h — True
    assert state.is_expiring_within(hours=2, now_ms=now_ms) is True
    # expires within 30 min — False
    assert state.is_expiring_within(hours=0.5, now_ms=now_ms) is False


def test_watch_state_already_expired():
    state = WatchState(history_id="1", expiration_ms=1, topic="t")
    assert state.is_expiring_within(hours=24, now_ms=1_000_000) is True


# ---- create_watch merges response into WatchState ---------------------


def test_create_watch_returns_raw_google_response():
    """create_watch returns the raw Gmail API response dict.
    Callers build a WatchState by merging with their local topic name.
    """
    service = _FakeService()
    service.users().watch_response = {
        "historyId": "99",
        "expiration": "1800000000000",
    }
    response = create_watch(service, topic_name="projects/x/topics/y")
    assert response == {"historyId": "99", "expiration": "1800000000000"}
