"""Tests for agents/shared/pubsub_pull.py.

Uses fake service objects matching the googleapiclient Pub/Sub v1 shape.
The real client is built via discovery ("pubsub", "v1"); these tests
bypass discovery and exercise the pure helpers against a Recorder.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
sys.path.insert(0, str(SHARED_DIR))

from pubsub_pull import (  # type: ignore  # noqa: E402
    GmailPushNotification,
    acknowledge_messages,
    decode_gmail_push,
    parse_pull_response,
    pull_messages,
)


# ---- fake googleapiclient service -------------------------------------


class _FakeExec:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeSubscriptions:
    def __init__(self):
        self.pull_calls: list[dict] = []
        self.ack_calls: list[dict] = []
        self.pull_response: dict = {"receivedMessages": []}
        self.ack_response: dict = {}

    def pull(self, subscription, body):
        self.pull_calls.append({"subscription": subscription, "body": body})
        return _FakeExec(self.pull_response)

    def acknowledge(self, subscription, body):
        self.ack_calls.append({"subscription": subscription, "body": body})
        return _FakeExec(self.ack_response)


class _FakeProjects:
    def __init__(self):
        self._subs = _FakeSubscriptions()

    def subscriptions(self):
        return self._subs


class _FakeService:
    def __init__(self):
        self._projects = _FakeProjects()

    def projects(self):
        return self._projects


def _encode_gmail_push(email: str, history_id: int) -> str:
    payload = json.dumps({"emailAddress": email, "historyId": history_id}).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


# ---- pull_messages ----------------------------------------------------


def test_pull_messages_posts_max_messages_and_ack_deadline():
    service = _FakeService()
    pull_messages(
        service,
        subscription_path="projects/foo/subscriptions/bar",
        max_messages=15,
    )
    subs = service.projects().subscriptions()
    assert len(subs.pull_calls) == 1
    assert subs.pull_calls[0]["subscription"] == "projects/foo/subscriptions/bar"
    assert subs.pull_calls[0]["body"]["maxMessages"] == 15
    # return_immediately=False is the Google-recommended default for long-poll
    assert subs.pull_calls[0]["body"]["returnImmediately"] is False


def test_pull_messages_returns_empty_when_no_messages():
    service = _FakeService()
    service.projects().subscriptions().pull_response = {}
    result = pull_messages(service, subscription_path="projects/x/subscriptions/y")
    assert result == []


def test_pull_messages_parses_received_messages():
    service = _FakeService()
    service.projects().subscriptions().pull_response = {
        "receivedMessages": [
            {
                "ackId": "ack-1",
                "message": {
                    "data": _encode_gmail_push("operator@example.com", 1234),
                    "messageId": "m1",
                    "publishTime": "2026-04-20T00:00:00Z",
                },
            },
            {
                "ackId": "ack-2",
                "message": {
                    "data": _encode_gmail_push("operator@example.com", 1235),
                    "messageId": "m2",
                    "publishTime": "2026-04-20T00:00:01Z",
                },
            },
        ],
    }
    result = pull_messages(service, subscription_path="projects/x/subscriptions/y")
    assert len(result) == 2
    assert result[0].ack_id == "ack-1"
    assert result[0].gmail_push.history_id == 1234
    assert result[0].gmail_push.email_address == "operator@example.com"
    assert result[1].ack_id == "ack-2"
    assert result[1].gmail_push.history_id == 1235


# ---- acknowledge_messages ---------------------------------------------


def test_acknowledge_posts_all_ack_ids():
    service = _FakeService()
    acknowledge_messages(
        service,
        subscription_path="projects/x/subscriptions/y",
        ack_ids=["a", "b", "c"],
    )
    acks = service.projects().subscriptions().ack_calls
    assert len(acks) == 1
    assert acks[0]["body"]["ackIds"] == ["a", "b", "c"]


def test_acknowledge_noop_on_empty_list():
    service = _FakeService()
    acknowledge_messages(service, subscription_path="p", ack_ids=[])
    assert service.projects().subscriptions().ack_calls == []


# ---- decode_gmail_push (pure) -----------------------------------------


def test_decode_gmail_push_happy():
    data = _encode_gmail_push("operator@example.com", 9999)
    push = decode_gmail_push(data)
    assert push.email_address == "operator@example.com"
    assert push.history_id == 9999


def test_decode_gmail_push_handles_string_history_id():
    payload = json.dumps({"emailAddress": "b@x.com", "historyId": "500"}).encode("utf-8")
    data = base64.b64encode(payload).decode("ascii")
    push = decode_gmail_push(data)
    assert push.history_id == 500


def test_decode_gmail_push_corrupt_returns_none():
    assert decode_gmail_push("not-base64!!!") is None
    assert decode_gmail_push(base64.b64encode(b"not json").decode()) is None
    # Missing required fields → None
    data = base64.b64encode(json.dumps({}).encode()).decode()
    assert decode_gmail_push(data) is None


# ---- parse_pull_response (pure) ---------------------------------------


def test_parse_pull_response_skips_unparseable_data():
    response = {
        "receivedMessages": [
            {"ackId": "good", "message": {"data": _encode_gmail_push("a@b.com", 1)}},
            {"ackId": "bad", "message": {"data": "garbage"}},
        ],
    }
    parsed = parse_pull_response(response)
    # bad rows are still returned with ack_id so they can be acked
    # (don't let them redeliver forever), but gmail_push is None
    assert len(parsed) == 2
    assert parsed[0].gmail_push is not None
    assert parsed[1].gmail_push is None


# ---- GmailPushNotification dataclass ---------------------------------


def test_gmail_push_notification_equality():
    a = GmailPushNotification(email_address="b@x.com", history_id=1)
    b = GmailPushNotification(email_address="b@x.com", history_id=1)
    assert a == b
