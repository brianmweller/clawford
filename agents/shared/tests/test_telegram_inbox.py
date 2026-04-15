"""Tests for agents/shared/telegram_inbox.py — the async long-polling
Telegram daemon.

We test the sync pieces directly (offset persistence, update extraction,
bot config loading) and the async poll_loop with a fake getUpdates client
that the test controls. Tests do NOT hit the real network and do NOT
spin up the full asyncio.gather fan-out.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    monkeypatch.setenv("CLAWFORD_INBOX_DIR", str(inbox_dir))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111111111")
    monkeypatch.setenv("FIXIT_BOT_TOKEN", "tok_fix")
    monkeypatch.setenv("SHOPPING_BOT_TOKEN", "tok_shop")

    for mod in list(sys.modules):
        if mod in ("telegram_inbox", "dispatcher", "conversation"):
            del sys.modules[mod]
    import telegram_inbox  # type: ignore
    return telegram_inbox


def test_load_offset_returns_zero_when_missing(inbox):
    assert inbox.load_offset("fix-it") == 0


def test_save_and_load_offset_roundtrip(inbox):
    inbox.save_offset("fix-it", 12345)
    assert inbox.load_offset("fix-it") == 12345


def test_kill_switch_file_blocks_daemon(inbox, tmp_path, monkeypatch):
    kill_path = tmp_path / "inbox-disabled"
    kill_path.write_text("off")
    monkeypatch.setattr(inbox, "KILL_SWITCH_PATH", str(kill_path))
    assert inbox.is_disabled() is True


def test_kill_switch_absent_allows_daemon(inbox, tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "KILL_SWITCH_PATH", str(tmp_path / "nope"))
    assert inbox.is_disabled() is False


def test_load_bot_configs_reads_env_and_returns_valid_pairs(inbox, monkeypatch):
    configs = inbox.load_bot_configs()
    ids = [c[0] for c in configs]
    assert "fix-it" in ids
    assert "shopping" in ids
    # Agents with no token env set are skipped.
    monkeypatch.delenv("SHOPPING_BOT_TOKEN", raising=False)
    reloaded = inbox.load_bot_configs()
    ids_after = [c[0] for c in reloaded]
    assert "fix-it" in ids_after
    assert "shopping" not in ids_after


def test_poll_loop_processes_updates_and_persists_offset(inbox):
    """Feed the poll loop a fake getUpdates response with two messages,
    verify both are dispatched and the offset advances past the max
    update_id."""
    dispatched: list[tuple] = []
    stop = asyncio.Event()

    updates_seq = [
        {
            "ok": True,
            "result": [
                {"update_id": 100, "message": {"from": {"id": 111111111},
                                                "chat": {"id": 111111111},
                                                "text": "hi"}},
                {"update_id": 101, "message": {"from": {"id": 111111111},
                                                "chat": {"id": 111111111},
                                                "text": "again"}},
            ],
        },
    ]

    async def fake_get_updates(token, offset, timeout):
        await asyncio.sleep(0)
        if updates_seq:
            return updates_seq.pop(0)
        # Second poll: signal stop and return empty.
        stop.set()
        return {"ok": True, "result": []}

    def fake_dispatch(agent_id, update):
        dispatched.append((agent_id, update["update_id"]))

    async def run():
        await asyncio.wait_for(
            inbox.poll_loop("fix-it", "tok_fix", fake_get_updates, fake_dispatch, stop),
            timeout=3.0,
        )

    asyncio.run(run())

    assert len(dispatched) == 2
    assert dispatched[0] == ("fix-it", 100)
    assert dispatched[1] == ("fix-it", 101)
    # Offset advanced to max + 1
    assert inbox.load_offset("fix-it") == 102


def test_poll_loop_survives_http_failures(inbox, monkeypatch):
    """If getUpdates raises, the loop must back off and keep trying —
    NOT crash the task and kill the whole daemon."""
    monkeypatch.setattr(inbox, "BACKOFF_S", 0.01)
    call_count = [0]
    stop = asyncio.Event()

    async def flaky_get_updates(token, offset, timeout):
        await asyncio.sleep(0)
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("network burp")
        # Second call: stop the loop and return clean
        stop.set()
        return {"ok": True, "result": []}

    async def run():
        await asyncio.wait_for(
            inbox.poll_loop("fix-it", "tok_fix", flaky_get_updates, lambda *a: None, stop),
            timeout=3.0,
        )

    asyncio.run(run())
    assert call_count[0] >= 2  # survived the first failure, kept going


def test_poll_loop_survives_dispatch_errors(inbox):
    """A crash inside dispatch must not break the poll loop — the next
    update gets processed normally."""
    stop = asyncio.Event()
    updates_seq = [
        {
            "ok": True,
            "result": [
                {"update_id": 200, "message": {"from": {"id": 111111111},
                                                "chat": {"id": 111111111},
                                                "text": "a"}},
                {"update_id": 201, "message": {"from": {"id": 111111111},
                                                "chat": {"id": 111111111},
                                                "text": "b"}},
            ],
        },
    ]

    async def fake_get_updates(token, offset, timeout):
        await asyncio.sleep(0)
        if updates_seq:
            return updates_seq.pop(0)
        stop.set()
        return {"ok": True, "result": []}

    processed: list[int] = []
    def dispatch_with_crash(agent_id, update):
        if update["update_id"] == 200:
            raise RuntimeError("boom")
        processed.append(update["update_id"])

    async def run():
        await asyncio.wait_for(
            inbox.poll_loop("fix-it", "tok_fix", fake_get_updates, dispatch_with_crash, stop),
            timeout=3.0,
        )

    asyncio.run(run())
    assert 201 in processed, "loop should have survived dispatch crash and processed the next update"
    # Offset advances past BOTH, even the one that crashed, so we don't
    # infinite-loop on a poison update.
    assert inbox.load_offset("fix-it") == 202
