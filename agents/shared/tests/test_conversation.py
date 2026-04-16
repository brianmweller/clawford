"""Tests for agents/shared/conversation.py — per-agent sliding window
conversation state, persisted to ~/.clawford/inbox/<agent>.jsonl.

Design invariants:
- Append-only JSONL on disk. Each line is one turn.
- In-memory load trims to the last WINDOW_SIZE entries.
- 1-hour inactivity timeout: if the last entry is >1h old, load()
  returns an empty window (the model should see a fresh session).
- Entries are arbitrary dicts with at least {ts, agent_id, item}.
  The `item` field is an OpenAI Responses input-item (either
  {role, content}, function_call, or function_call_output).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


@pytest.fixture
def conv_module(tmp_path, monkeypatch):
    """Point conversation state root at a tmp dir per test."""
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()

    # Force a reload so INBOX_DIR picks up the env override.
    for mod in list(sys.modules):
        if mod == "conversation" or mod.startswith("conversation."):
            del sys.modules[mod]

    monkeypatch.setenv("CLAWFORD_INBOX_DIR", str(inbox_dir))

    import conversation  # type: ignore
    return conversation


def _ts_offset(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_empty_agent_returns_empty_window(conv_module):
    items = conv_module.load("fix-it")
    assert items == []


def test_append_user_item_persists_to_jsonl(conv_module, tmp_path):
    conv_module.append("fix-it", {"role": "user", "content": "hi"})
    log_path = Path(conv_module.INBOX_DIR) / "fix-it.jsonl"
    assert log_path.exists()
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["agent_id"] == "fix-it"
    assert entry["item"] == {"role": "user", "content": "hi"}
    assert "ts" in entry


def test_load_returns_items_in_order(conv_module):
    conv_module.append("fix-it", {"role": "user", "content": "hi"})
    conv_module.append("fix-it", {"role": "assistant", "content": "hi back"})
    conv_module.append("fix-it", {"role": "user", "content": "how are you?"})

    items = conv_module.load("fix-it")
    assert len(items) == 3
    assert items[0]["content"] == "hi"
    assert items[1]["content"] == "hi back"
    assert items[2]["content"] == "how are you?"


def test_load_trims_to_window_size(conv_module):
    for i in range(30):
        conv_module.append("fix-it", {"role": "user", "content": f"msg {i}"})
    items = conv_module.load("fix-it")
    assert len(items) == conv_module.WINDOW_SIZE
    # Should be the MOST RECENT WINDOW_SIZE messages
    assert items[-1]["content"] == "msg 29"
    first_expected = 30 - conv_module.WINDOW_SIZE
    assert items[0]["content"] == f"msg {first_expected}"


def test_load_drops_window_after_1h_inactivity(conv_module, tmp_path):
    # Seed the log directly with a stale entry (>1h old).
    log_path = Path(conv_module.INBOX_DIR) / "fix-it.jsonl"
    stale = {
        "ts": _ts_offset(-3700),  # 1h 1m ago
        "agent_id": "fix-it",
        "item": {"role": "user", "content": "ancient"},
    }
    log_path.write_text(json.dumps(stale) + "\n")

    items = conv_module.load("fix-it")
    assert items == [], "stale window should be dropped on load"


def test_load_keeps_window_inside_1h(conv_module, tmp_path):
    log_path = Path(conv_module.INBOX_DIR) / "fix-it.jsonl"
    recent = {
        "ts": _ts_offset(-1500),  # 25m ago
        "agent_id": "fix-it",
        "item": {"role": "user", "content": "recent"},
    }
    log_path.write_text(json.dumps(recent) + "\n")

    items = conv_module.load("fix-it")
    assert len(items) == 1
    assert items[0]["content"] == "recent"


def test_append_function_call_and_output_roundtrip(conv_module):
    """Tool-use turns produce function_call + function_call_output items.
    The window must preserve them intact for multi-turn replay."""
    conv_module.append("fix-it", {"role": "user", "content": "how's the fleet?"})
    conv_module.append("fix-it", {
        "type": "function_call",
        "call_id": "call_abc",
        "name": "get_fleet_health",
        "arguments": "{}",
    })
    conv_module.append("fix-it", {
        "type": "function_call_output",
        "call_id": "call_abc",
        "output": "6 agents ok",
    })
    conv_module.append("fix-it", {"role": "assistant", "content": "All six agents ok."})

    items = conv_module.load("fix-it")
    assert len(items) == 4
    assert items[1]["type"] == "function_call"
    assert items[2]["type"] == "function_call_output"
    assert items[2]["output"] == "6 agents ok"


def test_load_drops_orphaned_function_call_outputs(conv_module):
    """When the WINDOW_SIZE trim lands mid-pair, a function_call_output
    without its paired function_call breaks the codex backend
    ('No tool call found for function call output'). The loader must
    drop orphan outputs so the window is internally consistent."""
    # Construct 40 items where the pair straddles the window boundary:
    # items 0-18: 19 user msgs — all trimmed off
    # item 19: function_call (JUST outside the window)
    # item 20: function_call_output (first item in window — orphan!)
    # items 21-39: 19 more user msgs
    for i in range(19):
        conv_module.append("fix-it", {"role": "user", "content": f"msg {i}"})
    conv_module.append("fix-it", {
        "type": "function_call", "call_id": "call_orphan_abc",
        "name": "get_x", "arguments": "{}",
    })
    conv_module.append("fix-it", {
        "type": "function_call_output", "call_id": "call_orphan_abc",
        "output": "ok",
    })
    for i in range(19, 38):
        conv_module.append("fix-it", {"role": "user", "content": f"msg {i}"})

    items = conv_module.load("fix-it")

    # Every function_call_output in the window must have a preceding
    # function_call with the same call_id.
    seen_calls: set[str] = set()
    for item in items:
        if item.get("type") == "function_call":
            seen_calls.add(item["call_id"])
        elif item.get("type") == "function_call_output":
            assert item["call_id"] in seen_calls, (
                f"orphaned function_call_output in window: {item}"
            )


def test_load_keeps_intact_function_call_pairs(conv_module):
    """Non-split pairs should pass through unchanged."""
    conv_module.append("fix-it", {"role": "user", "content": "hi"})
    conv_module.append("fix-it", {
        "type": "function_call", "call_id": "call_ok",
        "name": "get_x", "arguments": "{}",
    })
    conv_module.append("fix-it", {
        "type": "function_call_output", "call_id": "call_ok",
        "output": "result",
    })
    conv_module.append("fix-it", {"role": "assistant", "content": "done"})

    items = conv_module.load("fix-it")
    # All 4 items should be present in order
    assert len(items) == 4
    assert items[1]["call_id"] == "call_ok"
    assert items[2]["call_id"] == "call_ok"


def test_per_agent_isolation(conv_module):
    conv_module.append("fix-it", {"role": "user", "content": "fix-it msg"})
    conv_module.append("shopping", {"role": "user", "content": "shopping msg"})

    fix_it = conv_module.load("fix-it")
    shopping = conv_module.load("shopping")
    assert len(fix_it) == 1
    assert len(shopping) == 1
    assert fix_it[0]["content"] == "fix-it msg"
    assert shopping[0]["content"] == "shopping msg"


def test_malformed_jsonl_lines_are_skipped(conv_module, tmp_path):
    """Robustness: a corrupted or partially-written line must not crash
    the whole load."""
    log_path = Path(conv_module.INBOX_DIR) / "fix-it.jsonl"
    lines = [
        json.dumps({"ts": _ts_offset(-100), "agent_id": "fix-it",
                    "item": {"role": "user", "content": "good"}}),
        "not json at all",
        json.dumps({"ts": _ts_offset(-90), "agent_id": "fix-it",
                    "item": {"role": "user", "content": "good2"}}),
    ]
    log_path.write_text("\n".join(lines) + "\n")

    items = conv_module.load("fix-it")
    assert len(items) == 2
    assert items[0]["content"] == "good"
    assert items[1]["content"] == "good2"
