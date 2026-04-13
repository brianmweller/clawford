"""Tests for reminder-check.py host-cron Telegram send path.

Phase 2b of the cron migration: reminder-check.py now does its own
Telegram sends via urllib directly, and only marks a reminder as
"sent" in sent-reminders.json AFTER the POST returns success. This
moves the cron off the openclaw LLM dispatch queue and also fixes a
latent bug where failed LLM sends would still dedup the reminder,
preventing retry on the next tick.

These tests cover:
  1. The reminder message formatter (emoji + label + summary + minutes
     + optional location)
  2. The Telegram send helper (success, failure, missing token)
  3. The main() dedup-only-on-success behavior
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    """Load a hyphenated script as an underscore module. Stubs out the
    google auth libs so the module imports cleanly on test machines
    without them."""
    for mod in (
        "googleapiclient", "googleapiclient.discovery",
        "google", "google.auth", "google.auth.transport",
        "google.auth.transport.requests",
        "google.oauth2", "google.oauth2.credentials",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"famcal_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def rcheck():
    return _load_script("reminder-check.py")


# ─── message formatting ──────────────────────────────────────────────


def test_format_reminder_message_with_location(rcheck):
    reminder = {
        "calendar_emoji": "👨",
        "calendar_label": "Sam",
        "summary": "Dentist",
        "starts_in_min": 30,
        "location": "123 Main St",
        "tier": "30min",
    }
    msg = rcheck.format_reminder_message(reminder)
    assert msg == "🐭 Heads up — 👨 Sam Dentist in 30 min (123 Main St)"


def test_format_reminder_message_without_location(rcheck):
    reminder = {
        "calendar_emoji": "👩",
        "calendar_label": "Alex",
        "summary": "Yoga",
        "starts_in_min": 15,
        "location": "",
        "tier": "15min",
    }
    msg = rcheck.format_reminder_message(reminder)
    assert msg == "🐭 Heads up — 👩 Alex Yoga in 15 min"


def test_format_reminder_message_missing_emoji(rcheck):
    """Emoji is optional per calendar-config.json; fall back gracefully."""
    reminder = {
        "calendar_emoji": "",
        "calendar_label": "Sam",
        "summary": "Standup",
        "starts_in_min": 5,
        "location": "",
        "tier": "15min",
    }
    msg = rcheck.format_reminder_message(reminder)
    assert "🐭 Heads up — Sam Standup in 5 min" == msg


# ─── send_telegram helper ────────────────────────────────────────────


def test_send_telegram_returns_false_on_empty_token(rcheck):
    assert rcheck.send_telegram("", "111111111", "hi") is False


def test_send_telegram_returns_false_on_empty_chat_id(rcheck):
    assert rcheck.send_telegram("fake-token", "", "hi") is False


def test_send_telegram_returns_true_on_api_ok(rcheck):
    """Mock urllib.request.urlopen to return a body with ok:true."""
    with patch.object(rcheck, "urllib_request") as mock_ur:
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"ok":true,"result":{"message_id":123}}'
        mock_ur.urlopen.return_value = fake_resp
        mock_ur.Request = MagicMock(return_value="fake-req")

        result = rcheck.send_telegram("fake-token", "chat", "hello")
        assert result is True


def test_send_telegram_returns_false_on_api_error(rcheck):
    """Mock urlopen to raise — simulates network or 401 failure."""
    with patch.object(rcheck, "urllib_request") as mock_ur:
        mock_ur.urlopen.side_effect = Exception("connection refused")
        mock_ur.Request = MagicMock(return_value="fake-req")

        result = rcheck.send_telegram("fake-token", "chat", "hello")
        assert result is False


def test_send_telegram_returns_false_on_api_ok_false(rcheck):
    """Telegram API returned 200 but body.ok is false (e.g., chat blocked)."""
    with patch.object(rcheck, "urllib_request") as mock_ur:
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"ok":false,"description":"chat not found"}'
        mock_ur.urlopen.return_value = fake_resp
        mock_ur.Request = MagicMock(return_value="fake-req")

        result = rcheck.send_telegram("fake-token", "chat", "hello")
        assert result is False


# ─── main() dedup-on-success integration ─────────────────────────────


class _StubCtx:
    """Namespace bag the fixture hands back to the test. Attaching
    attributes to a Path fails on Windows, so use a plain container."""
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.next_events: list = []


@pytest.fixture
def stub_gcal(monkeypatch, tmp_path, rcheck):
    """Stub out Google Calendar + config + sent-reminders file so we
    can exercise main() end-to-end with a fake event list."""
    workspace = tmp_path / "family-calendar-workspace"
    workspace.mkdir()

    config = {
        "calendars": [
            {"id": "Sam@example.com", "label": "Sam", "emoji": "👨", "remind": True},
        ],
        "timezone": "America/Los_Angeles",
    }
    (workspace / "calendar-config.json").write_text(json.dumps(config))

    # Seed an empty sent-reminders file
    (workspace / "sent-reminders.json").write_text(json.dumps({
        "reminders": {},
        "last_pruned": datetime.now(timezone.utc).isoformat(),
    }))

    # Redirect module-level paths
    monkeypatch.setattr(rcheck, "WORKSPACE", str(workspace))
    monkeypatch.setattr(rcheck, "CONFIG_PATH", str(workspace / "calendar-config.json"))
    monkeypatch.setattr(rcheck, "REMINDERS_PATH", str(workspace / "sent-reminders.json"))
    monkeypatch.setattr(rcheck, "TOKEN_PATH", str(workspace / "token.json"))
    # Make token.json exist so get_credentials doesn't bail
    (workspace / "token.json").write_text(json.dumps({
        "token": "x", "refresh_token": "y",
        "token_uri": "z", "client_id": "a",
        "client_secret": "b", "scopes": ["cal"],
    }))

    ctx = _StubCtx(workspace)

    # Stub credentials + build so no real google client is constructed.
    fake_service = MagicMock()

    def fake_list(**kwargs):
        call_ctx = MagicMock()
        call_ctx.execute.return_value = {"items": ctx.next_events}
        return call_ctx

    fake_service.events.return_value.list.side_effect = fake_list

    monkeypatch.setattr(rcheck, "get_credentials", lambda: (MagicMock(), None))
    fake_build_mod = types.ModuleType("googleapiclient.discovery")
    fake_build_mod.build = MagicMock(return_value=fake_service)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", fake_build_mod)

    # No workflowy links (so no routing filter)
    monkeypatch.setenv("WORKFLOWY_LINKS_PATH", str(tmp_path / "nowhere.json"))

    return ctx


def _future_event(minutes: int, summary="Dentist", location="123 Main St"):
    start = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    return {
        "id": f"evt_{summary.replace(' ', '_')}_{minutes}",
        "status": "confirmed",
        "summary": summary,
        "location": location,
        "start": {"dateTime": start},
    }


def test_main_marks_reminder_sent_only_on_successful_telegram(rcheck, stub_gcal, capsys):
    """Reminder dedup must be conditional on the Telegram API returning
    ok. Old code marked sent BEFORE the LLM sent, so a failed send
    would still dedup and the user missed the reminder entirely.
    New code only adds to sent_reminders after send_telegram returns
    True."""
    stub_gcal.next_events = [_future_event(minutes=25)]  # inside 30min tier

    with patch.object(rcheck, "send_telegram", return_value=False):
        rcheck.main()

    # Reminder should NOT be marked sent because send_telegram failed
    with open(os.path.join(str(stub_gcal.workspace), "sent-reminders.json")) as f:
        data = json.load(f)
    assert data["reminders"] == {}, (
        f"failed send should NOT dedup, got: {data['reminders']}"
    )

    # Output should be SCRIPT_CONTRACT JSON with degraded status
    out = capsys.readouterr().out
    payload = json.loads(out.splitlines()[-1])
    assert payload["status"] in ("degraded", "error")
    assert payload.get("failed", 0) >= 1


def test_main_marks_reminder_sent_on_successful_telegram(rcheck, stub_gcal, capsys):
    stub_gcal.next_events = [_future_event(minutes=20)]

    with patch.object(rcheck, "send_telegram", return_value=True):
        rcheck.main()

    # Reminder SHOULD be marked sent
    with open(os.path.join(str(stub_gcal.workspace), "sent-reminders.json")) as f:
        data = json.load(f)
    assert len(data["reminders"]) == 1, (
        f"successful send should add to dedup, got: {data['reminders']}"
    )

    out = capsys.readouterr().out
    payload = json.loads(out.splitlines()[-1])
    assert payload["status"] == "ok"
    assert payload.get("sent", 0) == 1


def test_main_emits_script_contract_json_when_no_reminders(rcheck, stub_gcal, capsys):
    stub_gcal.next_events = []  # calendar is empty
    rcheck.main()
    out = capsys.readouterr().out
    payload = json.loads(out.splitlines()[-1])
    assert payload["status"] == "ok"
    assert payload.get("sent", 0) == 0
