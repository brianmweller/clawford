"""Tests for gmail-push-listener.py.

Core pure functions:
  - gmail_history_thread_ids: extracts new thread IDs + new cursor
    from a users.history.list() response.
  - process_thread_id: runs triage + compose as subprocesses.
  - run_tick: one full pull → history → subprocess → ack cycle.

Main loop + signal handling aren't tested (thin systemd wiring).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock


SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(AGENT_DIR / "scripts"))


def _load_listener():
    path = AGENT_DIR / "scripts" / "gmail-push-listener.py"
    spec = importlib.util.spec_from_file_location("gmail_push_listener", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- fake Gmail service (for history.list) ---------------------------


class _FakeExec:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class _FakeHistory:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeExec(self._response)


class _FakeGetProfileCall:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeExec(self._response)


class _FakeGmailUsers:
    def __init__(self, history_response, profile_response=None):
        self._history = _FakeHistory(history_response)
        self._profile = _FakeGetProfileCall(profile_response or {"historyId": "0"})

    def history(self):
        return self._history

    def getProfile(self, userId):  # noqa: N803
        return self._profile(userId=userId)


class _FakeGmailService:
    def __init__(self, history_response, profile_response=None):
        self._users = _FakeGmailUsers(history_response, profile_response)

    def users(self):
        return self._users


# ---- gmail_history_thread_ids ----------------------------------------


def test_gmail_history_thread_ids_extracts_messages_added():
    listener = _load_listener()
    response = {
        "history": [
            {
                "id": "1001",
                "messagesAdded": [
                    {"message": {"id": "m1", "threadId": "t100", "labelIds": ["INBOX"]}},
                    {"message": {"id": "m2", "threadId": "t100", "labelIds": ["INBOX"]}},
                ],
            },
            {
                "id": "1002",
                "messagesAdded": [
                    {"message": {"id": "m3", "threadId": "t101", "labelIds": ["INBOX"]}},
                ],
            },
        ],
        "historyId": "1002",
    }
    service = _FakeGmailService(response)
    thread_ids, new_cursor = listener.gmail_history_thread_ids(
        service, start_history_id="1000",
    )
    # t100 appears twice — dedup
    assert thread_ids == ["t100", "t101"]
    assert new_cursor == "1002"


def test_gmail_history_thread_ids_empty_response():
    listener = _load_listener()
    response = {"historyId": "1005"}  # no history array
    service = _FakeGmailService(response)
    thread_ids, new_cursor = listener.gmail_history_thread_ids(
        service, start_history_id="1005",
    )
    assert thread_ids == []
    assert new_cursor == "1005"


def test_gmail_history_thread_ids_passes_label_and_types():
    listener = _load_listener()
    service = _FakeGmailService({"historyId": "1"})
    listener.gmail_history_thread_ids(service, start_history_id="0")
    call = service.users().history().calls[0]
    assert call["userId"] == "me"
    assert call["startHistoryId"] == "0"
    assert call["labelId"] == "INBOX"
    assert call["historyTypes"] == ["messageAdded"]


def test_gmail_history_thread_ids_skips_non_inbox_messages():
    """messagesAdded can contain labels from labelFilterAction=include;
    drop anything without INBOX in labelIds."""
    listener = _load_listener()
    response = {
        "history": [
            {
                "id": "2",
                "messagesAdded": [
                    {"message": {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"]}},
                    {"message": {"id": "m2", "threadId": "t2", "labelIds": ["SPAM"]}},
                ],
            },
        ],
        "historyId": "2",
    }
    service = _FakeGmailService(response)
    thread_ids, _ = listener.gmail_history_thread_ids(
        service, start_history_id="1",
    )
    assert thread_ids == ["t1"]


# ---- process_thread_id -----------------------------------------------


def test_process_thread_id_runs_triage_then_compose(tmp_path):
    listener = _load_listener()

    runs: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        runs.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "inbox-triage.py").write_text("", encoding="utf-8")
    (scripts_dir / "auto-compose.py").write_text("", encoding="utf-8")

    result = listener.process_thread_id(
        thread_id="t42",
        scripts_dir=scripts_dir,
        runner=fake_runner,
    )

    assert len(runs) == 2
    # First invocation: inbox-triage.py --thread-id t42
    assert str(scripts_dir / "inbox-triage.py") in runs[0]
    assert "--thread-id" in runs[0]
    assert "t42" in runs[0]
    # Second invocation: auto-compose.py --force t42
    assert str(scripts_dir / "auto-compose.py") in runs[1]
    assert "--force" in runs[1]
    assert "t42" in runs[1]
    assert result["triage_rc"] == 0
    assert result["compose_rc"] == 0


def test_process_thread_id_skips_compose_when_triage_fails(tmp_path):
    listener = _load_listener()

    runs: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        runs.append(cmd)
        return MagicMock(returncode=2, stdout="", stderr="triage blew up")

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "inbox-triage.py").write_text("", encoding="utf-8")

    result = listener.process_thread_id(
        thread_id="t1",
        scripts_dir=scripts_dir,
        runner=fake_runner,
    )
    # Only the triage invocation happened
    assert len(runs) == 1
    assert result["triage_rc"] == 2
    assert result["compose_rc"] is None


# ---- run_tick (integration of pull + history + subprocess) -----------


def test_run_tick_processes_pulled_messages(tmp_path):
    listener = _load_listener()

    # Fake PubSub service: one received message
    import base64
    payload = json.dumps({"emailAddress": "b@x.com", "historyId": 1050}).encode()
    data_b64 = base64.b64encode(payload).decode("ascii")

    class _PubsubSubs:
        def __init__(self):
            self.pull_calls: list = []
            self.ack_calls: list = []

        def pull(self, subscription, body):
            self.pull_calls.append((subscription, body))
            return _FakeExec({
                "receivedMessages": [
                    {
                        "ackId": "ack-xyz",
                        "message": {
                            "data": data_b64,
                            "messageId": "m-1",
                            "publishTime": "2026-04-20T00:00:00Z",
                        },
                    },
                ],
            })

        def acknowledge(self, subscription, body):
            self.ack_calls.append((subscription, body))
            return _FakeExec({})

    class _PubsubProjects:
        def __init__(self):
            self._subs = _PubsubSubs()

        def subscriptions(self):
            return self._subs

    class _PubsubService:
        def __init__(self):
            self._projects = _PubsubProjects()

        def projects(self):
            return self._projects

    pubsub = _PubsubService()

    # Fake Gmail service: history.list returns one new thread
    history_resp = {
        "history": [
            {
                "id": "1050",
                "messagesAdded": [
                    {"message": {"id": "m1", "threadId": "tA", "labelIds": ["INBOX"]}},
                ],
            },
        ],
        "historyId": "1050",
    }
    gmail = _FakeGmailService(history_resp)

    cursor_path = tmp_path / "cursor.json"
    cursor_path.write_text(json.dumps({"history_id": "1000"}), encoding="utf-8")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "inbox-triage.py").write_text("", encoding="utf-8")
    (scripts_dir / "auto-compose.py").write_text("", encoding="utf-8")

    runs: list = []

    def fake_runner(cmd, **kwargs):
        runs.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    result = listener.run_tick(
        gmail_service=gmail,
        pubsub_service=pubsub,
        subscription_path="projects/p/subscriptions/s",
        cursor_path=cursor_path,
        scripts_dir=scripts_dir,
        runner=fake_runner,
    )

    # Message was pulled
    assert len(pubsub.projects().subscriptions().pull_calls) == 1
    # Thread was processed (triage + compose)
    assert len(runs) == 2
    # Message acked
    assert len(pubsub.projects().subscriptions().ack_calls) == 1
    assert pubsub.projects().subscriptions().ack_calls[0][1]["ackIds"] == ["ack-xyz"]
    # Cursor advanced
    saved = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert saved["history_id"] == "1050"
    # Summary
    assert result["pulled"] == 1
    assert result["threads_processed"] == 1
    assert result["cursor"] == "1050"


def test_run_tick_empty_pull_is_noop(tmp_path):
    listener = _load_listener()

    class _NoopSubs:
        def pull(self, subscription, body):
            return _FakeExec({})

        def acknowledge(self, subscription, body):
            raise AssertionError("should not ack when nothing pulled")

    class _NoopProjects:
        def subscriptions(self):
            return _NoopSubs()

    class _NoopPubsub:
        def projects(self):
            return _NoopProjects()

    cursor_path = tmp_path / "cursor.json"
    cursor_path.write_text(json.dumps({"history_id": "1000"}), encoding="utf-8")

    gmail = _FakeGmailService({"historyId": "1000"})

    result = listener.run_tick(
        gmail_service=gmail,
        pubsub_service=_NoopPubsub(),
        subscription_path="p/s",
        cursor_path=cursor_path,
        scripts_dir=tmp_path,
        runner=lambda *_a, **_k: None,
    )
    assert result["pulled"] == 0
    assert result["threads_processed"] == 0


def test_run_tick_handles_unparseable_message_by_acking(tmp_path):
    """A Pub/Sub message that isn't a Gmail push notification (e.g.
    corrupt base64 or JSON) must still be acked so it doesn't redeliver
    forever."""
    listener = _load_listener()

    class _BadSubs:
        def __init__(self):
            self.ack_calls = []

        def pull(self, subscription, body):
            return _FakeExec({
                "receivedMessages": [
                    {"ackId": "ack-bad", "message": {"data": "not-base64!!!"}},
                ],
            })

        def acknowledge(self, subscription, body):
            self.ack_calls.append(body)
            return _FakeExec({})

    class _BadProjects:
        def __init__(self):
            self._subs = _BadSubs()

        def subscriptions(self):
            return self._subs

    class _BadPubsub:
        def __init__(self):
            self._projects = _BadProjects()

        def projects(self):
            return self._projects

    pubsub = _BadPubsub()
    cursor_path = tmp_path / "cursor.json"
    cursor_path.write_text(json.dumps({"history_id": "1"}), encoding="utf-8")

    result = listener.run_tick(
        gmail_service=_FakeGmailService({"historyId": "1"}),
        pubsub_service=pubsub,
        subscription_path="p/s",
        cursor_path=cursor_path,
        scripts_dir=tmp_path,
        runner=lambda *_a, **_k: None,
    )
    # bad message was still acked
    assert pubsub.projects().subscriptions().ack_calls[0]["ackIds"] == ["ack-bad"]
    assert result["unparseable"] == 1


# ---- cursor round-trip -----------------------------------------------


def test_load_cursor_reads_existing(tmp_path):
    listener = _load_listener()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"history_id": "555"}), encoding="utf-8")
    assert listener.load_cursor(p) == "555"


def test_load_cursor_missing_returns_none(tmp_path):
    listener = _load_listener()
    assert listener.load_cursor(tmp_path / "nope.json") is None


def test_save_cursor_atomic(tmp_path):
    listener = _load_listener()
    p = tmp_path / "c.json"
    listener.save_cursor(p, "777")
    assert json.loads(p.read_text(encoding="utf-8"))["history_id"] == "777"
