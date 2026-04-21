"""Tests for agents/connector/scripts/facts_callback_lib.py — the
single entry point the dispatcher calls when the operator taps a Telegram
button on a brain-maintenance item.

Contract:
- handle_facts_callback(action, arg, *, facts_dir, queue_path, now_iso)
  routes to the right operation and returns a structured result dict.
- Never raises; error paths return {"status": "error", "detail": ...}.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "facts_callback_lib", _SCRIPTS_DIR / "facts_callback_lib.py"
)
cblib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cblib)


_SAMPLE_PENDING = """\
# Pending review — low-confidence mined facts

---
- **id:** connector-jane-doe-xyz
- **subject:** jane-doe
- **category:** preference
- **confidence:** 0.45
- **audience_scope:** ["personal"]
- **source:** gmail:msg-abc
- **reason:** low-conf extraction
- **content:** Jane prefers tea over coffee.
---
- **id:** connector-alex-reyes-abc
- **subject:** alex-reyes
- **category:** event
- **confidence:** 0.5
- **audience_scope:** ["professional"]
- **source:** gmail:alex-msg
- **content:** Alex mentioned a new client.
"""


def _seed(tmp_path: Path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "_pending_review.md").write_text(_SAMPLE_PENDING, encoding="utf-8")

    queue_path = tmp_path / "queue.jsonl"
    queue_path.write_text(
        json.dumps({
            "id": "connector-jane-doe-xyz",
            "source": "miner",
            "fact": {"subject": "jane-doe", "content": "Jane prefers tea over coffee."},
        }) + "\n"
        + json.dumps({
            "id": "connector-alex-reyes-abc",
            "source": "miner",
            "fact": {"subject": "alex-reyes", "content": "Alex mentioned a new client."},
        }) + "\n",
        encoding="utf-8",
    )
    return facts_dir, queue_path


def test_approve_promotes_fact_and_removes_from_queue(tmp_path: Path):
    facts_dir, queue_path = _seed(tmp_path)
    result = cblib.handle_facts_callback(
        "approve", "connector-jane-doe-xyz",
        facts_dir=facts_dir, queue_path=queue_path,
        now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "ok"
    assert result["action"] == "approve"
    # Fact landed in April file
    month = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "connector-jane-doe-xyz" in month
    assert "- **confidence:** 0.95" in month
    # Queue no longer carries this entry
    lines = queue_path.read_text(encoding="utf-8").splitlines()
    ids = [json.loads(l)["id"] for l in lines]
    assert "connector-jane-doe-xyz" not in ids


def test_reject_writes_rejected_file_and_removes_from_queue(tmp_path: Path):
    facts_dir, queue_path = _seed(tmp_path)
    result = cblib.handle_facts_callback(
        "reject", "connector-jane-doe-xyz",
        facts_dir=facts_dir, queue_path=queue_path,
        now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "ok"
    assert result["action"] == "reject"
    rejected = (facts_dir / "_rejected.md").read_text(encoding="utf-8")
    assert "connector-jane-doe-xyz" in rejected
    # Removed from queue
    lines = queue_path.read_text(encoding="utf-8").splitlines()
    ids = [json.loads(l)["id"] for l in lines]
    assert "connector-jane-doe-xyz" not in ids


def test_skip_mutes_entry_in_queue_for_seven_days(tmp_path: Path):
    facts_dir, queue_path = _seed(tmp_path)
    result = cblib.handle_facts_callback(
        "skip", "connector-jane-doe-xyz",
        facts_dir=facts_dir, queue_path=queue_path,
        now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "ok"
    assert result["action"] == "skip"
    # Entry is still in the queue but now carries muted_until
    entries = [json.loads(l) for l in queue_path.read_text(encoding="utf-8").splitlines()]
    by_id = {e["id"]: e for e in entries}
    assert "muted_until" in by_id["connector-jane-doe-xyz"]
    assert "connector-alex-reyes-abc" in by_id  # still there, unmuted
    # muted_until is approximately 7 days out
    assert by_id["connector-jane-doe-xyz"]["muted_until"].startswith("2026-04-28")


def test_approve_returns_not_found_for_missing_entry(tmp_path: Path):
    facts_dir, queue_path = _seed(tmp_path)
    result = cblib.handle_facts_callback(
        "approve", "ghost-id",
        facts_dir=facts_dir, queue_path=queue_path,
        now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "not_found"


def test_unknown_action_returns_error(tmp_path: Path):
    facts_dir, queue_path = _seed(tmp_path)
    result = cblib.handle_facts_callback(
        "detonate", "any-id",
        facts_dir=facts_dir, queue_path=queue_path,
        now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "error"
    assert "unknown action" in result.get("detail", "").lower()


def test_approve_remaining_approves_all_active_queue_entries(tmp_path: Path):
    facts_dir, queue_path = _seed(tmp_path)
    result = cblib.handle_facts_callback(
        "approve_remaining", "batch-123",
        facts_dir=facts_dir, queue_path=queue_path,
        now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "ok"
    assert result["action"] == "approve_remaining"
    assert result["approved_count"] == 2
    month = (facts_dir / "2026-04.md").read_text(encoding="utf-8")
    assert "connector-jane-doe-xyz" in month
    assert "connector-alex-reyes-abc" in month
    # Queue now empty
    assert queue_path.read_text(encoding="utf-8").strip() == ""


def test_silence_today_mutes_all_active_queue_entries(tmp_path: Path):
    facts_dir, queue_path = _seed(tmp_path)
    result = cblib.handle_facts_callback(
        "silence_today", "batch-123",
        facts_dir=facts_dir, queue_path=queue_path,
        now_iso="2026-04-21T18:00:00Z",
    )
    assert result["status"] == "ok"
    assert result["action"] == "silence_today"
    assert result["silenced_count"] == 2
    entries = [json.loads(l) for l in queue_path.read_text(encoding="utf-8").splitlines()]
    for e in entries:
        assert "muted_until" in e
        # Silence-today mutes until the NEXT morning (just "tomorrow morning")
        assert e["muted_until"].startswith("2026-04-22")
