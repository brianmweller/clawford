"""Tests for brain-maintenance-digest.py — the morning-brief section that
surfaces pending-review queue items as tap-to-resolve Telegram messages.

Rendering is pure (takes a queue-entry list, returns a message list). The
I/O-heavy main() is exercised indirectly via the pure helpers.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "brain_maintenance_digest", _SCRIPTS_DIR / "brain-maintenance-digest.py"
)
digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(digest)


def _queue_entry(
    entry_id: str,
    *,
    subject: str = "jane-doe",
    content: str = "Jane prefers tea.",
    confidence: float = 0.55,
    source: str = "miner",
) -> dict:
    return {
        "id": entry_id,
        "source": source,
        "fact": {
            "id": entry_id,
            "subject": subject,
            "content": content,
            "confidence": confidence,
            "category": "preference",
            "source_detail": "gmail:msg-xyz",
        },
        "question": "Is this a durable fact worth remembering?",
        "options": ["approve", "reject", "skip"],
    }


# ─── Rendering ───────────────────────────────────────────────────────


def test_render_returns_empty_when_no_entries():
    assert digest.render_digest_messages([], batch_id="b1") == []


def test_render_one_message_per_item_for_small_batch():
    entries = [_queue_entry(f"x-{i}") for i in range(3)]
    msgs = digest.render_digest_messages(entries, batch_id="b1")
    # 3 per-item + 1 bulk footer (since >= 3 items)
    assert len(msgs) == 4
    per_item = msgs[:3]
    for m in per_item:
        text = m["text"]
        # Must include the subject and the content
        assert "jane-doe" in text.lower() or "Jane" in text
        # Must carry inline keyboard
        kb = m["reply_markup"]["inline_keyboard"]
        # At least one row with three buttons (Yes / No / Skip)
        button_texts = [btn["text"] for row in kb for btn in row]
        assert any("Yes" in t for t in button_texts)
        assert any("No" in t for t in button_texts)
        assert any("Skip" in t for t in button_texts)
        # Callbacks are facts:<action>:<id>
        callback_prefixes = [btn["callback_data"] for row in kb for btn in row]
        assert any(cb.startswith("facts:approve:") for cb in callback_prefixes)
        assert any(cb.startswith("facts:reject:") for cb in callback_prefixes)
        assert any(cb.startswith("facts:skip:") for cb in callback_prefixes)


def test_render_item_callback_carries_entry_id():
    entry = _queue_entry("pending-abc-123")
    msgs = digest.render_digest_messages([entry], batch_id="b1")
    # 1 item + no bulk footer (only 1 item, < 3)
    assert len(msgs) == 1
    kb = msgs[0]["reply_markup"]["inline_keyboard"]
    callbacks = {btn["callback_data"] for row in kb for btn in row}
    assert "facts:approve:pending-abc-123" in callbacks
    assert "facts:reject:pending-abc-123" in callbacks
    assert "facts:skip:pending-abc-123" in callbacks


def test_render_bulk_footer_appears_when_three_or_more_items():
    entries = [_queue_entry(f"x-{i}") for i in range(3)]
    msgs = digest.render_digest_messages(entries, batch_id="bulk-1")
    footer = msgs[-1]
    assert "remaining" in footer["text"].lower() or "silence" in footer["text"].lower()
    cb = {btn["callback_data"] for row in footer["reply_markup"]["inline_keyboard"] for btn in row}
    assert "facts:approve_remaining:bulk-1" in cb
    assert "facts:silence_today:bulk-1" in cb


def test_render_no_bulk_footer_for_two_items():
    entries = [_queue_entry(f"x-{i}") for i in range(2)]
    msgs = digest.render_digest_messages(entries, batch_id="b1")
    # Exactly 2 per-item messages, no footer
    assert len(msgs) == 2
    for m in msgs:
        cb = {btn["callback_data"] for row in m["reply_markup"]["inline_keyboard"] for btn in row}
        assert not any(c.startswith("facts:approve_remaining") for c in cb)


def test_render_collapses_to_summary_for_large_batch():
    entries = [_queue_entry(f"x-{i}") for i in range(8)]
    msgs = digest.render_digest_messages(entries, batch_id="big")
    # Summary mode: exactly one message
    assert len(msgs) == 1
    text = msgs[0]["text"]
    # Mentions the count
    assert "8" in text
    assert "pending" in text.lower() or "review" in text.lower()
    # Has an Expand button carrying the batch_id
    cb = {btn["callback_data"] for row in msgs[0]["reply_markup"]["inline_keyboard"] for btn in row}
    assert "facts:expand:big" in cb


def test_render_summary_threshold_is_six():
    # 5 → per-item (5 messages + bulk footer = 6 total)
    five = [_queue_entry(f"x-{i}") for i in range(5)]
    assert len(digest.render_digest_messages(five, batch_id="b5")) >= 5
    # 6 → summary (1 message)
    six = [_queue_entry(f"x-{i}") for i in range(6)]
    msgs6 = digest.render_digest_messages(six, batch_id="b6")
    assert len(msgs6) == 1


# ─── Confidence + source-aware wording ───────────────────────────────


def test_render_mentions_confidence_for_low_conf_facts():
    entry = _queue_entry("x", confidence=0.45)
    msg = digest.render_digest_messages([entry], batch_id="b")[0]
    assert "0.45" in msg["text"] or "45" in msg["text"]


def test_render_labels_migration_source():
    entry = _queue_entry("x", source="migration")
    msg = digest.render_digest_messages([entry], batch_id="b")[0]
    assert "migration" in msg["text"].lower() or "classif" in msg["text"].lower()


def test_render_labels_dedupe_source():
    entry = _queue_entry("x", source="dedupe")
    msg = digest.render_digest_messages([entry], batch_id="b")[0]
    assert "dedup" in msg["text"].lower() or "dup" in msg["text"].lower() or "merge" in msg["text"].lower()
