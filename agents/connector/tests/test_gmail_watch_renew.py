"""Tests for gmail-watch-renew.py.

The script has three layers:
  1. run_renewal(service, topic, state_path) — pure orchestration.
     Takes a pre-built Gmail service + topic + state path. Calls
     create_watch, persists state, returns a result dict. Tests stub
     the service.
  2. main() — CLI entrypoint. Not covered here (thin wiring of
     argparse + credentials + service build).
  3. Contract envelope wrapper — inherited from the generic main-wrap
     pattern used across the fleet; also not covered here.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(AGENT_DIR / "scripts"))


def _load_renew():
    path = AGENT_DIR / "scripts" / "gmail-watch-renew.py"
    spec = importlib.util.spec_from_file_location("gmail_watch_renew", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- fake Gmail service ----------------------------------------------


class _FakeExec:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class _FakeUsers:
    def __init__(self, response):
        self.calls: list[dict] = []
        self._response = response

    def watch(self, userId, body):  # noqa: N803
        self.calls.append({"userId": userId, "body": body})
        return _FakeExec(self._response)


class _FakeService:
    def __init__(self, watch_response):
        self._users = _FakeUsers(watch_response)

    def users(self):
        return self._users


def test_run_renewal_calls_watch_and_saves_state(tmp_path):
    renew = _load_renew()
    service = _FakeService({
        "historyId": "777",
        "expiration": str(1_800_000_000_000),
    })
    state_path = tmp_path / "watch.json"

    result = renew.run_renewal(
        service=service,
        topic="projects/p/topics/gmail-huckle-inbox",
        state_path=state_path,
    )

    # Gmail called
    calls = service.users().calls
    assert len(calls) == 1
    assert calls[0]["body"]["topicName"] == "projects/p/topics/gmail-huckle-inbox"
    assert calls[0]["body"]["labelIds"] == ["INBOX"]

    # State persisted
    assert state_path.exists()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["history_id"] == "777"
    assert saved["expiration_ms"] == 1_800_000_000_000
    assert saved["topic"] == "projects/p/topics/gmail-huckle-inbox"

    # Result dict summarizes the renewal
    assert result["status"] == "ok"
    assert result["history_id"] == "777"
    assert result["expiration_ms"] == 1_800_000_000_000
    assert result["topic"] == "projects/p/topics/gmail-huckle-inbox"


def test_run_renewal_raises_on_missing_expiration(tmp_path):
    """A Gmail response missing historyId or expiration indicates a
    server-side contract break — raise so the contract envelope shows
    status=error on Telegram."""
    renew = _load_renew()
    service = _FakeService({"historyId": "1"})  # no expiration
    state_path = tmp_path / "watch.json"

    try:
        renew.run_renewal(
            service=service,
            topic="projects/p/topics/t",
            state_path=state_path,
        )
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError on missing expiration")


def test_run_renewal_overwrites_prior_state(tmp_path):
    renew = _load_renew()
    state_path = tmp_path / "watch.json"
    # Seed prior state
    state_path.write_text(json.dumps({
        "history_id": "1",
        "expiration_ms": 1,
        "topic": "old-topic",
    }), encoding="utf-8")

    service = _FakeService({
        "historyId": "999",
        "expiration": "2_000_000_000_000".replace("_", ""),
    })
    renew.run_renewal(
        service=service,
        topic="projects/p/topics/new-topic",
        state_path=state_path,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["history_id"] == "999"
    assert saved["topic"] == "projects/p/topics/new-topic"
