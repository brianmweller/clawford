"""Tests for reminder-check.py task-ping integration.

reminder-check.py is extended to also scan ``tasks/queue.md`` and emit
Telegram pings for tasks hitting the T-30min or T+24h-overdue tiers.
Event reminders and task reminders share the ``sent-reminders.json``
dedup cache; task dedup keys are ``{task_id}_{tier}``.

Task pings carry an inline keyboard:
    ✅ done · ⏭ snooze · 🚫 ignore
These map to ``callback_data`` strings ``task_done:{id}``,
``task_snooze:{id}``, ``task_ignore:{id}`` — handled by the live
dispatcher in ``agents/shared/dispatcher.py``.

These tests cover:
  1. format_task_reminder_message — body text for T-30min and T+24h
  2. build_task_keyboard — inline_keyboard JSON shape
  3. send_telegram with reply_markup — payload extension
  4. main() — tasks read from brain, pings sent, dedup persisted
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SHARED_DIR = Path(__file__).resolve().parents[2] / "shared"
AGENT_DIR = Path(__file__).resolve().parent.parent


def _stub_google_libs():
    for mod in (
        "googleapiclient", "googleapiclient.discovery",
        "google", "google.auth", "google.auth.transport",
        "google.auth.transport.requests",
        "google.oauth2", "google.oauth2.credentials",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    # reminder-check.py does `from googleapiclient.discovery import build` —
    # the stubbed module needs a callable attribute or the import fails.
    disc = sys.modules["googleapiclient.discovery"]

    def _fake_build(*_args, **_kwargs):
        svc = MagicMock()
        svc.events.return_value.list.return_value.execute.return_value = {"items": []}
        return svc

    disc.build = _fake_build


def _load_script(name: str):
    _stub_google_libs()
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"famcal_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def rcheck(monkeypatch, tmp_path):
    # Make sure brain_tasks and task_surfacer resolve
    sys.path.insert(0, str(SHARED_DIR))
    sys.path.insert(0, str(AGENT_DIR))
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))
    (tmp_path / "brain" / "tasks").mkdir(parents=True)
    for mod in ("brain", "brain_tasks", "task_surfacer"):
        sys.modules.pop(mod, None)
    return _load_script("reminder-check.py")


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def test_format_task_reminder_message_t_30min(rcheck):
    payload = {
        "task_id": "a-1",
        "tier": "t_30min",
        "description": "Pay quarterly tax estimate",
        "due_at": "2026-04-18T17:00:00Z",
    }
    msg = rcheck.format_task_reminder_message(payload)
    assert "📋" in msg
    assert "Pay quarterly tax estimate" in msg
    assert "30 min" in msg or "in 30" in msg


def test_format_task_reminder_message_overdue(rcheck):
    payload = {
        "task_id": "a-1",
        "tier": "t_24h_overdue",
        "description": "File expense report",
        "due_at": "2026-04-17T17:00:00Z",
    }
    msg = rcheck.format_task_reminder_message(payload)
    assert "File expense report" in msg
    assert "overdue" in msg.lower()


# ---------------------------------------------------------------------------
# Inline keyboard
# ---------------------------------------------------------------------------


def test_build_task_keyboard_shape(rcheck):
    kb = rcheck.build_task_keyboard("family-calendar-2026-04-18-001")
    assert "inline_keyboard" in kb
    rows = kb["inline_keyboard"]
    assert len(rows) == 1
    row = rows[0]
    assert len(row) == 3
    labels = [b["text"] for b in row]
    assert any("done" in t.lower() for t in labels)
    assert any("snooze" in t.lower() for t in labels)
    assert any("ignore" in t.lower() for t in labels)
    data = [b["callback_data"] for b in row]
    assert "task_done:family-calendar-2026-04-18-001" in data
    assert "task_snooze:family-calendar-2026-04-18-001" in data
    assert "task_ignore:family-calendar-2026-04-18-001" in data


# ---------------------------------------------------------------------------
# send_telegram accepts reply_markup
# ---------------------------------------------------------------------------


def test_send_telegram_includes_reply_markup_when_provided(rcheck):
    kb = rcheck.build_task_keyboard("a-1")
    captured = {}

    class FakeResp:
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=0):
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    with patch.object(rcheck.urllib_request, "urlopen", side_effect=fake_urlopen):
        ok = rcheck.send_telegram("TOKEN", "CHAT", "hello", reply_markup=kb)
    assert ok is True
    assert captured["data"]["reply_markup"] == kb


def test_send_telegram_without_reply_markup_omits_key(rcheck):
    captured = {}

    class FakeResp:
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=0):
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    with patch.object(rcheck.urllib_request, "urlopen", side_effect=fake_urlopen):
        rcheck.send_telegram("TOKEN", "CHAT", "hello")
    assert "reply_markup" not in captured["data"]


# ---------------------------------------------------------------------------
# main() — tasks surface through the reminder path
# ---------------------------------------------------------------------------


def _seed_queue(brain_root: Path, content: str) -> None:
    (brain_root / "tasks" / "queue.md").write_text(content, encoding="utf-8")


def _seed_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "calendar-config.json").write_text(json.dumps({"calendars": []}), encoding="utf-8")
    # Token file contents won't matter — we stub get_credentials
    (ws / "token.json").write_text("{}", encoding="utf-8")
    return ws


def test_main_emits_task_reminder_with_inline_keyboard(rcheck, tmp_path, monkeypatch):
    brain_root = Path(os.environ["CLAWFORD_BRAIN_DROPBOX_ROOT"])
    ws = _seed_workspace(tmp_path)
    monkeypatch.setattr(rcheck, "WORKSPACE", str(ws))
    monkeypatch.setattr(rcheck, "REMINDERS_PATH", str(ws / "sent-reminders.json"))
    monkeypatch.setattr(rcheck, "CONFIG_PATH", str(ws / "calendar-config.json"))
    monkeypatch.setattr(rcheck, "TOKEN_PATH", str(ws / "token.json"))

    # Timed task due in ~20 min — inside the T-30min window
    due_utc = datetime.now(timezone.utc) + timedelta(minutes=20)
    due_iso = due_utc.isoformat().replace("+00:00", "Z")
    content = (
        "# Tasks — Queue\n\n---\n\n"
        "## family-calendar-2026-04-18-001\n"
        "- description: Pay quarterly tax estimate\n"
        "- assignee: me\n"
        "- status: open\n"
        f"- due_at: {due_iso}\n"
        "- source_agent: family-calendar\n"
        "- created_at: 2026-04-18T09:00:00Z\n\n"
    )
    _seed_queue(brain_root, content)

    # Stub google credentials + service so we don't need real calendars
    monkeypatch.setattr(rcheck, "get_credentials", lambda: (MagicMock(), None))
    monkeypatch.setenv("FAMILYCAL_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")

    captured_payloads = []

    class FakeResp:
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=0):
        captured_payloads.append(json.loads(req.data.decode("utf-8")))
        return FakeResp()

    with patch.object(rcheck.urllib_request, "urlopen", side_effect=fake_urlopen):
        rcheck.main()

    # One task ping sent with the inline keyboard
    assert len(captured_payloads) == 1
    p = captured_payloads[0]
    assert "Pay quarterly tax estimate" in p["text"]
    assert "reply_markup" in p
    assert any(
        b["callback_data"] == "task_done:family-calendar-2026-04-18-001"
        for b in p["reply_markup"]["inline_keyboard"][0]
    )

    # Dedup persisted
    sent = json.loads((ws / "sent-reminders.json").read_text())
    assert "family-calendar-2026-04-18-001_t_30min" in sent["reminders"]


def test_main_does_not_resend_already_dedupd_task(rcheck, tmp_path, monkeypatch):
    brain_root = Path(os.environ["CLAWFORD_BRAIN_DROPBOX_ROOT"])
    ws = _seed_workspace(tmp_path)
    monkeypatch.setattr(rcheck, "WORKSPACE", str(ws))
    monkeypatch.setattr(rcheck, "REMINDERS_PATH", str(ws / "sent-reminders.json"))
    monkeypatch.setattr(rcheck, "CONFIG_PATH", str(ws / "calendar-config.json"))
    monkeypatch.setattr(rcheck, "TOKEN_PATH", str(ws / "token.json"))

    due_utc = datetime.now(timezone.utc) + timedelta(minutes=20)
    due_iso = due_utc.isoformat().replace("+00:00", "Z")
    content = (
        "# Tasks — Queue\n\n---\n\n"
        "## a-1\n"
        "- description: Already pinged\n"
        "- assignee: me\n"
        "- status: open\n"
        f"- due_at: {due_iso}\n"
        "- source_agent: family-calendar\n"
        "- created_at: 2026-04-18T09:00:00Z\n\n"
    )
    _seed_queue(brain_root, content)

    # Pre-populate sent-reminders.json as if we already fired T-30min
    (ws / "sent-reminders.json").write_text(
        json.dumps({"reminders": {"a-1_t_30min": "2026-04-18T16:30:00Z"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(rcheck, "get_credentials", lambda: (MagicMock(), None))
    monkeypatch.setenv("FAMILYCAL_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")

    captured = []

    class FakeResp:
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=0):
        captured.append(json.loads(req.data.decode("utf-8")))
        return FakeResp()

    with patch.object(rcheck.urllib_request, "urlopen", side_effect=fake_urlopen):
        rcheck.main()

    assert captured == []


def test_main_does_not_send_for_non_me_or_non_open_tasks(rcheck, tmp_path, monkeypatch):
    brain_root = Path(os.environ["CLAWFORD_BRAIN_DROPBOX_ROOT"])
    ws = _seed_workspace(tmp_path)
    monkeypatch.setattr(rcheck, "WORKSPACE", str(ws))
    monkeypatch.setattr(rcheck, "REMINDERS_PATH", str(ws / "sent-reminders.json"))
    monkeypatch.setattr(rcheck, "CONFIG_PATH", str(ws / "calendar-config.json"))
    monkeypatch.setattr(rcheck, "TOKEN_PATH", str(ws / "token.json"))

    due_utc = datetime.now(timezone.utc) + timedelta(minutes=20)
    due_iso = due_utc.isoformat().replace("+00:00", "Z")
    content = (
        "# Tasks — Queue\n\n---\n\n"
        "## a-1\n"
        "- description: Wife's task\n"
        "- assignee: wife\n"
        "- status: open\n"
        f"- due_at: {due_iso}\n"
        "- source_agent: family-calendar\n"
        "- created_at: 2026-04-18T09:00:00Z\n\n"
        "## a-2\n"
        "- description: Already done\n"
        "- assignee: me\n"
        "- status: done\n"
        f"- due_at: {due_iso}\n"
        "- source_agent: family-calendar\n"
        "- created_at: 2026-04-18T09:00:00Z\n\n"
    )
    _seed_queue(brain_root, content)

    monkeypatch.setattr(rcheck, "get_credentials", lambda: (MagicMock(), None))
    monkeypatch.setenv("FAMILYCAL_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")

    captured = []

    class FakeResp:
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=0):
        captured.append(json.loads(req.data.decode("utf-8")))
        return FakeResp()

    with patch.object(rcheck.urllib_request, "urlopen", side_effect=fake_urlopen):
        rcheck.main()

    assert captured == []
