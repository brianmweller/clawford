"""Tests for agents/shared/pending_queue.py — the append-only JSONL queue
that carries brain-maintenance items (low-conf facts, suspected dupes,
migration ambiguities) between producers (miners) and the morning-brief
digest.

Contract:
- append(entry) writes one JSONL line, atomically (tmp + rename-append).
- load_active(now) returns entries whose muted_until is absent or < now.
- find_by_id(id) returns the entry dict or None.
- mute(id, until) sets muted_until on that entry; atomic rewrite.
- remove(id) deletes that entry from the file; atomic rewrite.
- Missing file / empty file → [] (never raises).
- Malformed lines are skipped with a logged warning, not raised.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import pending_queue  # type: ignore


# ─── append ──────────────────────────────────────────────────────────

def test_append_creates_file_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    entry = {"id": "x-1", "source": "miner", "fact": {"subject": "jane-doe"}}
    pending_queue.append(path, entry)
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "x-1"


def test_append_appends_to_existing(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    pending_queue.append(path, {"id": "x-1"})
    pending_queue.append(path, {"id": "x-2"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(l)["id"] for l in lines] == ["x-1", "x-2"]


def test_append_is_idempotent_on_same_id(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    pending_queue.append(path, {"id": "x-1", "source": "miner"})
    pending_queue.append(path, {"id": "x-1", "source": "miner"})  # dup
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ─── load / load_active ──────────────────────────────────────────────

def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert pending_queue.load(tmp_path / "absent.jsonl") == []


def test_load_returns_all_entries_in_order(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text(
        json.dumps({"id": "a"}) + "\n" + json.dumps({"id": "b"}) + "\n",
        encoding="utf-8",
    )
    entries = pending_queue.load(path)
    assert [e["id"] for e in entries] == ["a", "b"]


def test_load_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text(
        json.dumps({"id": "ok-1"}) + "\n"
        "this is not json\n"
        + json.dumps({"id": "ok-2"}) + "\n",
        encoding="utf-8",
    )
    entries = pending_queue.load(path)
    assert [e["id"] for e in entries] == ["ok-1", "ok-2"]


def test_load_active_filters_muted_entries(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    path.write_text(
        json.dumps({"id": "active"}) + "\n"
        + json.dumps({"id": "muted", "muted_until": future}) + "\n"
        + json.dumps({"id": "unmuted-expired", "muted_until": past}) + "\n",
        encoding="utf-8",
    )
    active = pending_queue.load_active(path)
    ids = {e["id"] for e in active}
    assert ids == {"active", "unmuted-expired"}


# ─── find_by_id ──────────────────────────────────────────────────────

def test_find_by_id_returns_entry(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    pending_queue.append(path, {"id": "x-1", "source": "miner"})
    pending_queue.append(path, {"id": "x-2", "source": "migration"})
    entry = pending_queue.find_by_id(path, "x-2")
    assert entry is not None and entry["source"] == "migration"


def test_find_by_id_returns_none_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    pending_queue.append(path, {"id": "x-1"})
    assert pending_queue.find_by_id(path, "ghost") is None


# ─── mute / remove ───────────────────────────────────────────────────

def test_mute_sets_muted_until(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    pending_queue.append(path, {"id": "x-1", "source": "miner"})
    pending_queue.append(path, {"id": "x-2", "source": "miner"})
    until = "2099-01-01T00:00:00+00:00"
    pending_queue.mute(path, "x-1", until)
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    by_id = {e["id"]: e for e in lines}
    assert by_id["x-1"]["muted_until"] == until
    assert "muted_until" not in by_id["x-2"]


def test_remove_deletes_entry_preserves_others(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    pending_queue.append(path, {"id": "keep-1"})
    pending_queue.append(path, {"id": "drop-me"})
    pending_queue.append(path, {"id": "keep-2"})
    pending_queue.remove(path, "drop-me")
    ids = [json.loads(l)["id"] for l in path.read_text(encoding="utf-8").splitlines()]
    assert ids == ["keep-1", "keep-2"]


def test_remove_noop_when_id_absent(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    pending_queue.append(path, {"id": "a"})
    pending_queue.remove(path, "ghost")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ─── atomicity ───────────────────────────────────────────────────────

def test_mute_uses_atomic_rewrite(tmp_path: Path) -> None:
    # Smoke test — after mute there must be no .tmp leftover
    path = tmp_path / "q.jsonl"
    pending_queue.append(path, {"id": "x-1"})
    pending_queue.mute(path, "x-1", "2099-01-01T00:00:00+00:00")
    assert not (path.with_suffix(".jsonl.tmp")).exists()
