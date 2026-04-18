"""Tests for gcal-tasks-sync.py — two-way sync between queue.md and
Google Tasks.

queue.md is canonical for descriptions and timing; GCal Tasks is the
mobile surface where the operator checks things off from his phone. The sync
script runs every 5 min from a host cron.

Covered here:
  - find_list_id — match "Sam.M.Smith" by title, hard-fail if missing
  - build_create_payload — ISO due + title; [IGNORED] prefix when ignored
  - build_patch_payload — diff against last-pushed; no-op when unchanged
  - push pass — create / patch / delete mutates state map
  - pull pass — completed-on-phone, deleted-on-phone, unchecked-from-ignored
  - reconciliation — unmapped remote tasks logged + skipped
  - list-not-found — emits alert, hard-fails

The Google Tasks API client is swapped for a FakeTasksService that
tracks list/task state in memory and validates the calls we care about.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "agents" / "family-calendar" / "scripts" / "gcal-tasks-sync.py"
SHARED_DIR = REPO_ROOT / "agents" / "shared"
AGENT_DIR = REPO_ROOT / "agents" / "family-calendar"


@pytest.fixture
def sync_mod(monkeypatch, tmp_path):
    # Google libs are lazy-imported inside main()/get_credentials, so the
    # module loads without stubs. The public functions under test
    # (find_list_id, build_create_payload, push_pass, pull_pass) operate
    # on a service object passed in by the test; no real Google import
    # needed. Avoiding the stubs keeps sibling tests (heartbeat probes
    # google_oauth) from tripping over polluted sys.modules.
    for p in (str(SHARED_DIR), str(AGENT_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    monkeypatch.setenv("CLAWFORD_BRAIN_DROPBOX_ROOT", str(tmp_path / "brain"))
    (tmp_path / "brain" / "tasks").mkdir(parents=True)
    for mod in ("brain", "brain_tasks", "gcal_tasks_sync", "famcal_gcal_tasks_sync"):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location("famcal_gcal_tasks_sync", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------------------
# Fake Google Tasks service
# ---------------------------------------------------------------------------


class FakeTasksService:
    """In-memory Google Tasks double. Tracks lists + tasks; records
    insert/patch/delete calls for assertion. Mirrors googleapiclient's
    ``service.tasklists()`` / ``service.tasks()`` resource-chain shape."""

    def __init__(self):
        self._lists: dict[str, dict] = {}
        self._task_store: dict[str, dict[str, dict]] = {}
        self.inserted: list[tuple] = []
        self.patched: list[tuple] = []
        self.deleted: list[tuple] = []

    def add_list(self, list_id: str, title: str) -> None:
        self._lists[list_id] = {"id": list_id, "title": title}
        self._task_store.setdefault(list_id, {})

    def add_remote_task(self, list_id: str, **fields) -> dict:
        tid = fields.get("id") or f"remote-{uuid4().hex[:8]}"
        fields["id"] = tid
        fields.setdefault("status", "needsAction")
        fields.setdefault("updated", _iso_now())
        self._task_store.setdefault(list_id, {})[tid] = fields
        return fields

    def tasklists(self):
        outer = self

        class _TL:
            def list(self, **kwargs):
                class _R:
                    def execute(_):
                        return {"items": list(outer._lists.values())}
                return _R()
        return _TL()

    def tasks(self):
        outer = self

        class _T:
            def list(self, *, tasklist, **kwargs):
                class _R:
                    def execute(_):
                        items = list(outer._task_store.get(tasklist, {}).values())
                        if not kwargs.get("showDeleted", False):
                            items = [t for t in items if not t.get("deleted")]
                        if not kwargs.get("showCompleted", True):
                            items = [t for t in items if t.get("status") != "completed"]
                        return {"items": items}
                return _R()

            def insert(self, *, tasklist, body):
                class _R:
                    def execute(_):
                        tid = f"remote-{uuid4().hex[:8]}"
                        t = dict(body)
                        t["id"] = tid
                        t["updated"] = _iso_now()
                        outer._task_store.setdefault(tasklist, {})[tid] = t
                        outer.inserted.append((tasklist, t))
                        return t
                return _R()

            def patch(self, *, tasklist, task, body):
                class _R:
                    def execute(_):
                        cur = outer._task_store.get(tasklist, {}).get(task)
                        if cur is None:
                            raise RuntimeError("no such task")
                        cur.update(body)
                        cur["updated"] = _iso_now()
                        outer.patched.append((tasklist, task, body))
                        return cur
                return _R()

            def delete(self, *, tasklist, task):
                class _R:
                    def execute(_):
                        outer._task_store.get(tasklist, {}).pop(task, None)
                        outer.deleted.append((tasklist, task))
                        return None
                return _R()
        return _T()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Sample tasks
# ---------------------------------------------------------------------------


def _brain_task(tid, *, description="thing", status="open", due_at=None, assignee="me"):
    import brain_tasks  # type: ignore
    return brain_tasks.Task(
        id=tid,
        description=description,
        assignee=assignee,
        status=status,
        source_agent="family-calendar",
        created_at="2026-04-18T09:00:00Z",
        due_at=due_at,
    )


# ---------------------------------------------------------------------------
# find_list_id
# ---------------------------------------------------------------------------


def test_find_list_id_matches_by_title(sync_mod):
    svc = FakeTasksService()
    svc.add_list("abc", "Sam.M.Smith")
    svc.add_list("xyz", "Groceries")
    assert sync_mod.find_list_id(svc, "Sam.M.Smith") == "abc"


def test_find_list_id_returns_none_when_missing(sync_mod):
    svc = FakeTasksService()
    svc.add_list("xyz", "Groceries")
    assert sync_mod.find_list_id(svc, "Sam.M.Smith") is None


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def test_build_create_payload_timed(sync_mod):
    t = _brain_task("a-1", description="Pay taxes", due_at="2026-04-18T17:00:00Z")
    payload = sync_mod.build_create_payload(t)
    assert payload["title"] == "Pay taxes"
    assert payload["due"] == "2026-04-18T17:00:00Z"
    assert payload["status"] == "needsAction"


def test_build_create_payload_all_day_omits_time(sync_mod):
    t = _brain_task("a-1", description="Water plants", due_at="2026-04-18")
    payload = sync_mod.build_create_payload(t)
    # Google Tasks API expects RFC 3339; promote date-only to midnight UTC
    assert payload["due"].startswith("2026-04-18T00:00:00")


def test_build_create_payload_ignored_adds_prefix(sync_mod):
    t = _brain_task("a-1", description="Ugh not doing this", status="ignored")
    payload = sync_mod.build_create_payload(t)
    assert payload["title"].startswith("[IGNORED] ")
    assert payload["status"] == "completed"


def test_build_patch_payload_returns_none_when_unchanged(sync_mod):
    t = _brain_task("a-1", description="Same", due_at="2026-04-18T17:00:00Z")
    entry = {
        "last_pushed_status": "open",
        "last_pushed_due_at": "2026-04-18T17:00:00Z",
        "last_pushed_description_hash": sync_mod.hash_description("Same"),
    }
    assert sync_mod.build_patch_payload(t, entry) is None


def test_build_patch_payload_detects_description_change(sync_mod):
    t = _brain_task("a-1", description="New wording", due_at="2026-04-18T17:00:00Z")
    entry = {
        "last_pushed_status": "open",
        "last_pushed_due_at": "2026-04-18T17:00:00Z",
        "last_pushed_description_hash": sync_mod.hash_description("Old wording"),
    }
    p = sync_mod.build_patch_payload(t, entry)
    assert p is not None
    assert p["title"] == "New wording"


def test_build_patch_payload_detects_due_change(sync_mod):
    t = _brain_task("a-1", description="Same", due_at="2026-04-18T18:00:00Z")
    entry = {
        "last_pushed_status": "open",
        "last_pushed_due_at": "2026-04-18T17:00:00Z",
        "last_pushed_description_hash": sync_mod.hash_description("Same"),
    }
    p = sync_mod.build_patch_payload(t, entry)
    assert p is not None
    assert p["due"] == "2026-04-18T18:00:00Z"


def test_build_patch_payload_detects_status_done(sync_mod):
    t = _brain_task("a-1", description="Same", due_at="2026-04-18T17:00:00Z", status="done")
    entry = {
        "last_pushed_status": "open",
        "last_pushed_due_at": "2026-04-18T17:00:00Z",
        "last_pushed_description_hash": sync_mod.hash_description("Same"),
    }
    p = sync_mod.build_patch_payload(t, entry)
    assert p is not None
    assert p["status"] == "completed"


def test_build_patch_payload_detects_status_ignored_adds_prefix(sync_mod):
    t = _brain_task("a-1", description="Nope", status="ignored")
    entry = {
        "last_pushed_status": "open",
        "last_pushed_due_at": None,
        "last_pushed_description_hash": sync_mod.hash_description("Nope"),
    }
    p = sync_mod.build_patch_payload(t, entry)
    assert p is not None
    assert p["status"] == "completed"
    assert p["title"].startswith("[IGNORED] ")


# ---------------------------------------------------------------------------
# Push pass
# ---------------------------------------------------------------------------


def test_push_creates_unmapped_open_task(sync_mod, tmp_path):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    state = sync_mod.new_state("L1")
    import brain_tasks  # type: ignore
    brain_tasks.append_task(_brain_task("a-1", description="New task", due_at="2026-04-18T17:00:00Z"))
    sync_mod.push_pass(svc, state, brain_tasks.read_tasks())
    assert len(svc.inserted) == 1
    assert "a-1" in state["map"]
    assert state["map"]["a-1"]["last_pushed_status"] == "open"


def test_push_skips_assigned_to_others(sync_mod, tmp_path):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    state = sync_mod.new_state("L1")
    import brain_tasks  # type: ignore
    brain_tasks.append_task(_brain_task("a-1", description="Wife's task", assignee="wife", due_at="2026-04-18"))
    sync_mod.push_pass(svc, state, brain_tasks.read_tasks())
    assert svc.inserted == []
    assert "a-1" not in state["map"]


def test_push_patches_when_description_changed(sync_mod, tmp_path):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    svc.add_remote_task("L1", id="GT-1", title="Old wording", status="needsAction", due="2026-04-18T17:00:00Z")
    state = sync_mod.new_state("L1")
    state["map"]["a-1"] = {
        "gcal_task_id": "GT-1",
        "gcal_updated": _iso_now(),
        "last_pushed_status": "open",
        "last_pushed_due_at": "2026-04-18T17:00:00Z",
        "last_pushed_description_hash": sync_mod.hash_description("Old wording"),
        "last_synced_at": _iso_now(),
        "tombstoned": False,
    }
    import brain_tasks  # type: ignore
    brain_tasks.append_task(_brain_task("a-1", description="New wording", due_at="2026-04-18T17:00:00Z"))
    sync_mod.push_pass(svc, state, brain_tasks.read_tasks())
    assert len(svc.patched) == 1
    assert svc.patched[0][2]["title"] == "New wording"


def test_push_deletes_remote_when_local_status_deleted(sync_mod, tmp_path):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    svc.add_remote_task("L1", id="GT-1", title="Zombie")
    state = sync_mod.new_state("L1")
    state["map"]["a-1"] = {
        "gcal_task_id": "GT-1",
        "gcal_updated": _iso_now(),
        "last_pushed_status": "open",
        "last_pushed_due_at": None,
        "last_pushed_description_hash": sync_mod.hash_description("Zombie"),
        "last_synced_at": _iso_now(),
        "tombstoned": False,
    }
    import brain_tasks  # type: ignore
    brain_tasks.append_task(_brain_task("a-1", description="Zombie", status="deleted"))
    sync_mod.push_pass(svc, state, brain_tasks.read_tasks(include_deleted=True))
    assert svc.deleted == [("L1", "GT-1")]
    assert state["map"]["a-1"]["tombstoned"] is True


# ---------------------------------------------------------------------------
# Pull pass — conflict matrix rows
# ---------------------------------------------------------------------------


def test_pull_completed_on_phone_flips_local_to_done(sync_mod, tmp_path):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    svc.add_remote_task("L1", id="GT-1", title="Pay taxes", status="completed", completed="2026-04-18T17:05:00Z")
    state = sync_mod.new_state("L1")
    state["map"]["a-1"] = {
        "gcal_task_id": "GT-1",
        "gcal_updated": "2026-04-18T16:00:00Z",
        "last_pushed_status": "open",
        "last_pushed_due_at": "2026-04-18T17:00:00Z",
        "last_pushed_description_hash": sync_mod.hash_description("Pay taxes"),
        "last_synced_at": "2026-04-18T16:00:00Z",
        "tombstoned": False,
    }
    import brain_tasks  # type: ignore
    brain_tasks.append_task(_brain_task("a-1", description="Pay taxes", due_at="2026-04-18T17:00:00Z"))
    sync_mod.pull_pass(svc, state)
    assert brain_tasks.read_tasks()[0].status == "done"
    assert state["map"]["a-1"]["last_pushed_status"] == "done"


def test_pull_deleted_on_phone_tombstones_local(sync_mod, tmp_path):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    svc.add_remote_task("L1", id="GT-1", title="Gone", deleted=True, status="needsAction")
    state = sync_mod.new_state("L1")
    state["map"]["a-1"] = {
        "gcal_task_id": "GT-1",
        "gcal_updated": "2026-04-18T16:00:00Z",
        "last_pushed_status": "open",
        "last_pushed_due_at": None,
        "last_pushed_description_hash": sync_mod.hash_description("Gone"),
        "last_synced_at": "2026-04-18T16:00:00Z",
        "tombstoned": False,
    }
    import brain_tasks  # type: ignore
    brain_tasks.append_task(_brain_task("a-1", description="Gone"))
    sync_mod.pull_pass(svc, state)
    # Tombstone propagated — status=deleted appended
    tasks = brain_tasks.read_tasks(include_deleted=True)
    assert tasks[0].status == "deleted"


def test_pull_unignored_flips_local_back_to_open(sync_mod, tmp_path):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    svc.add_remote_task("L1", id="GT-1", title="[IGNORED] Never doing this", status="needsAction")
    state = sync_mod.new_state("L1")
    state["map"]["a-1"] = {
        "gcal_task_id": "GT-1",
        "gcal_updated": "2026-04-18T16:00:00Z",
        "last_pushed_status": "ignored",
        "last_pushed_due_at": None,
        "last_pushed_description_hash": sync_mod.hash_description("Never doing this"),
        "last_synced_at": "2026-04-18T16:00:00Z",
        "tombstoned": False,
    }
    import brain_tasks  # type: ignore
    brain_tasks.append_task(_brain_task("a-1", description="Never doing this", status="ignored"))
    sync_mod.pull_pass(svc, state)
    assert brain_tasks.read_tasks()[0].status == "open"


def test_pull_skips_unmapped_remote_tasks(sync_mod, tmp_path, capsys):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    svc.add_remote_task("L1", id="GT-X", title="Created on phone", status="needsAction")
    state = sync_mod.new_state("L1")
    # No local task for GT-X
    sync_mod.pull_pass(svc, state)
    # Nothing added to queue — nothing to crash on
    import brain_tasks  # type: ignore
    assert brain_tasks.read_tasks() == []


def test_pull_noop_when_both_sides_already_done(sync_mod, tmp_path):
    svc = FakeTasksService()
    svc.add_list("L1", "Sam.M.Smith")
    svc.add_remote_task("L1", id="GT-1", title="Done already", status="completed")
    state = sync_mod.new_state("L1")
    state["map"]["a-1"] = {
        "gcal_task_id": "GT-1",
        "gcal_updated": "2026-04-18T16:00:00Z",
        "last_pushed_status": "done",
        "last_pushed_due_at": None,
        "last_pushed_description_hash": sync_mod.hash_description("Done already"),
        "last_synced_at": "2026-04-18T16:00:00Z",
        "tombstoned": False,
    }
    import brain_tasks  # type: ignore
    brain_tasks.append_task(_brain_task("a-1", description="Done already", status="done"))
    sync_mod.pull_pass(svc, state)
    # No status change
    assert brain_tasks.read_tasks()[0].status == "done"
