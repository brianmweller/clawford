"""Tests for agents/shared/pending_actions.py — the shared pending action store.

Covers: stage, load, load_by_id, load_by_batch, remove, assign_batch,
prune_expired. Thread safety of remove (double-tap). Expiry filtering.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))


@pytest.fixture
def pa(tmp_path, monkeypatch):
    """Fresh pending_actions module with workspace rooted in tmp_path."""
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    for mod in list(sys.modules):
        if mod == "pending_actions":
            del sys.modules[mod]
    import pending_actions
    return pending_actions


@pytest.fixture
def workspace(tmp_path):
    """Pre-create the workspace directory for an agent."""
    ws = tmp_path / "shopping-workspace"
    ws.mkdir()
    return ws


# ── stage ────────────────────────────────────────────────────────


def test_stage_creates_action_with_id_and_expiry(pa, workspace):
    result = pa.stage(
        "shopping", "reorder",
        payload={"source": "costco", "item_number": "1914462", "quantity": 1},
        summary="Add 1x Kirkland Water to Costco cart",
        confirm_label="\U0001f6d2 Add to cart",
        cancel_label="Skip",
    )

    assert "__pending_action__" in result
    assert "id" in result["__pending_action__"]
    action_id = result["__pending_action__"]["id"]
    assert action_id.startswith("act_")
    assert result["action_id"] == action_id
    assert result["summary"] == "Add 1x Kirkland Water to Costco cart"

    # Verify it's persisted
    actions = pa.load("shopping")
    assert len(actions) == 1
    assert actions[0]["id"] == action_id
    assert actions[0]["kind"] == "reorder"
    assert actions[0]["payload"]["source"] == "costco"
    assert actions[0]["confirm_label"] == "\U0001f6d2 Add to cart"
    assert actions[0]["cancel_label"] == "Skip"

    # Check expiry is ~4 hours from now
    expires = datetime.fromisoformat(actions[0]["expires_at"])
    now = datetime.now(timezone.utc)
    delta = expires - now
    assert 3.9 * 3600 < delta.total_seconds() < 4.1 * 3600


def test_stage_multiple_actions_appends(pa, workspace):
    pa.stage("shopping", "reorder", {"item": "a"}, "Item A",
             confirm_label="Confirm", cancel_label="Cancel")
    pa.stage("shopping", "reorder", {"item": "b"}, "Item B",
             confirm_label="Confirm", cancel_label="Cancel")
    pa.stage("shopping", "reorder", {"item": "c"}, "Item C",
             confirm_label="Confirm", cancel_label="Cancel")

    actions = pa.load("shopping")
    assert len(actions) == 3
    summaries = {a["summary"] for a in actions}
    assert summaries == {"Item A", "Item B", "Item C"}


def test_stage_custom_ttl(pa, workspace):
    result = pa.stage(
        "shopping", "reorder", {"item": "x"}, "Custom TTL",
        confirm_label="OK", cancel_label="No",
        ttl_hours=1,
    )
    actions = pa.load("shopping")
    expires = datetime.fromisoformat(actions[0]["expires_at"])
    now = datetime.now(timezone.utc)
    delta = expires - now
    assert 0.9 * 3600 < delta.total_seconds() < 1.1 * 3600


# ── load ─────────────────────────────────────────────────────────


def test_load_returns_empty_for_no_file(pa):
    assert pa.load("nonexistent") == []


def test_load_returns_only_non_expired_actions(pa, workspace):
    pa.stage("shopping", "reorder", {"item": "fresh"}, "Fresh",
             confirm_label="OK", cancel_label="No")

    # Manually inject an expired action
    file_path = pa._pending_path("shopping")
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)
    expired = {
        "id": "act_expired000",
        "kind": "reorder",
        "agent_id": "shopping",
        "batch_id": None,
        "staged_at": "2020-01-01T00:00:00+00:00",
        "expires_at": "2020-01-01T04:00:00+00:00",
        "summary": "Old",
        "confirm_label": "OK",
        "cancel_label": "No",
        "payload": {},
    }
    data["actions"].append(expired)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    actions = pa.load("shopping")
    assert len(actions) == 1
    assert actions[0]["summary"] == "Fresh"


# ── load_by_id ───────────────────────────────────────────────────


def test_load_by_id_returns_action(pa, workspace):
    result = pa.stage("shopping", "reorder", {"item": "x"}, "Find me",
                      confirm_label="OK", cancel_label="No")
    action_id = result["action_id"]

    action = pa.load_by_id("shopping", action_id)
    assert action is not None
    assert action["id"] == action_id
    assert action["summary"] == "Find me"


def test_load_by_id_returns_none_for_missing(pa, workspace):
    assert pa.load_by_id("shopping", "act_doesnotexist") is None


def test_load_by_id_returns_none_for_expired(pa, workspace):
    result = pa.stage("shopping", "reorder", {"item": "x"}, "Soon expired",
                      confirm_label="OK", cancel_label="No")
    action_id = result["action_id"]

    # Manually expire it
    file_path = pa._pending_path("shopping")
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)
    data["actions"][0]["expires_at"] = "2020-01-01T00:00:00+00:00"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    assert pa.load_by_id("shopping", action_id) is None


# ── remove ───────────────────────────────────────────────────────


def test_remove_returns_action_and_deletes(pa, workspace):
    result = pa.stage("shopping", "reorder", {"item": "x"}, "Remove me",
                      confirm_label="OK", cancel_label="No")
    action_id = result["action_id"]

    removed = pa.remove("shopping", action_id)
    assert removed is not None
    assert removed["id"] == action_id
    assert removed["summary"] == "Remove me"

    # Verify it's gone
    assert pa.load("shopping") == []
    assert pa.load_by_id("shopping", action_id) is None


def test_remove_returns_none_for_missing(pa, workspace):
    assert pa.remove("shopping", "act_nope") is None


def test_remove_double_tap_second_returns_none(pa, workspace):
    result = pa.stage("shopping", "reorder", {"item": "x"}, "Once only",
                      confirm_label="OK", cancel_label="No")
    action_id = result["action_id"]

    first = pa.remove("shopping", action_id)
    assert first is not None

    second = pa.remove("shopping", action_id)
    assert second is None


# ── assign_batch ─────────────────────────────────────────────────


def test_assign_batch_writes_batch_id(pa, workspace):
    r1 = pa.stage("shopping", "reorder", {"item": "a"}, "A",
                  confirm_label="OK", cancel_label="No")
    r2 = pa.stage("shopping", "reorder", {"item": "b"}, "B",
                  confirm_label="OK", cancel_label="No")
    r3 = pa.stage("shopping", "reorder", {"item": "c"}, "C",
                  confirm_label="OK", cancel_label="No")

    ids = [r1["action_id"], r2["action_id"], r3["action_id"]]
    count = pa.assign_batch("shopping", ids, "batch_test123")
    assert count == 3

    actions = pa.load("shopping")
    for a in actions:
        assert a["batch_id"] == "batch_test123"


def test_assign_batch_partial_ids(pa, workspace):
    r1 = pa.stage("shopping", "reorder", {"item": "a"}, "A",
                  confirm_label="OK", cancel_label="No")
    r2 = pa.stage("shopping", "reorder", {"item": "b"}, "B",
                  confirm_label="OK", cancel_label="No")

    count = pa.assign_batch("shopping", [r1["action_id"], "act_nope"], "batch_x")
    assert count == 1

    actions = pa.load("shopping")
    batched = [a for a in actions if a["batch_id"] == "batch_x"]
    assert len(batched) == 1
    assert batched[0]["id"] == r1["action_id"]


# ── load_by_batch ────────────────────────────────────────────────


def test_load_by_batch_returns_matching(pa, workspace):
    r1 = pa.stage("shopping", "reorder", {"item": "a"}, "A",
                  confirm_label="OK", cancel_label="No")
    r2 = pa.stage("shopping", "reorder", {"item": "b"}, "B",
                  confirm_label="OK", cancel_label="No")
    r3 = pa.stage("shopping", "reorder", {"item": "c"}, "C",
                  confirm_label="OK", cancel_label="No")

    pa.assign_batch("shopping", [r1["action_id"], r2["action_id"]], "batch_ab")

    batch = pa.load_by_batch("shopping", "batch_ab")
    assert len(batch) == 2
    ids = {a["id"] for a in batch}
    assert ids == {r1["action_id"], r2["action_id"]}


def test_load_by_batch_empty_for_unknown(pa, workspace):
    assert pa.load_by_batch("shopping", "batch_nope") == []


# ── prune_expired ────────────────────────────────────────────────


def test_prune_expired_removes_stale(pa, workspace):
    pa.stage("shopping", "reorder", {"item": "fresh"}, "Fresh",
             confirm_label="OK", cancel_label="No")

    # Inject an expired action
    file_path = pa._pending_path("shopping")
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)
    data["actions"].append({
        "id": "act_old",
        "kind": "reorder",
        "agent_id": "shopping",
        "batch_id": None,
        "staged_at": "2020-01-01T00:00:00+00:00",
        "expires_at": "2020-01-01T04:00:00+00:00",
        "summary": "Stale",
        "confirm_label": "OK",
        "cancel_label": "No",
        "payload": {},
    })
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    pruned = pa.prune_expired("shopping")
    assert pruned == 1

    actions = pa.load("shopping")
    assert len(actions) == 1
    assert actions[0]["summary"] == "Fresh"


def test_prune_expired_returns_zero_when_nothing_stale(pa, workspace):
    pa.stage("shopping", "reorder", {"item": "x"}, "Ok",
             confirm_label="OK", cancel_label="No")
    assert pa.prune_expired("shopping") == 0


def test_prune_expired_on_empty(pa):
    assert pa.prune_expired("nonexistent") == 0


# ── thread safety ────────────────────────────────────────────────


def test_concurrent_remove_only_one_wins(pa, workspace):
    """Two threads try to remove the same action. Only one should succeed."""
    result = pa.stage("shopping", "reorder", {"item": "x"}, "Race",
                      confirm_label="OK", cancel_label="No")
    action_id = result["action_id"]

    results = []

    def do_remove():
        r = pa.remove("shopping", action_id)
        results.append(r)

    t1 = threading.Thread(target=do_remove)
    t2 = threading.Thread(target=do_remove)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    wins = [r for r in results if r is not None]
    losses = [r for r in results if r is None]
    assert len(wins) == 1
    assert len(losses) == 1


# ── load_latest ──────────────────────────────────────────────────


def test_load_latest_returns_none_when_empty(pa):
    assert pa.load_latest("nonexistent") is None


def test_load_latest_returns_most_recent(pa, workspace):
    pa.stage("shopping", "reorder", {"item": "a"}, "First",
             confirm_label="OK", cancel_label="No")
    time.sleep(0.01)  # ensure staged_at differs
    pa.stage("shopping", "reorder", {"item": "b"}, "Second",
             confirm_label="OK", cancel_label="No")
    time.sleep(0.01)
    r3 = pa.stage("shopping", "skip_sns", {"sub": "abc"}, "Third",
                  confirm_label="OK", cancel_label="No")

    latest = pa.load_latest("shopping")
    assert latest is not None
    assert latest["id"] == r3["action_id"]
    assert latest["summary"] == "Third"


def test_load_latest_filters_by_kind(pa, workspace):
    r1 = pa.stage("shopping", "reorder", {"item": "a"}, "Reorder A",
                  confirm_label="OK", cancel_label="No")
    time.sleep(0.01)
    r2 = pa.stage("shopping", "skip_sns", {"sub": "x"}, "Skip X",
                  confirm_label="OK", cancel_label="No")
    time.sleep(0.01)
    r3 = pa.stage("shopping", "reorder", {"item": "b"}, "Reorder B",
                  confirm_label="OK", cancel_label="No")

    # latest overall is Reorder B
    assert pa.load_latest("shopping")["id"] == r3["action_id"]
    # latest of kind=skip_sns is Skip X
    latest_skip = pa.load_latest("shopping", kind="skip_sns")
    assert latest_skip is not None
    assert latest_skip["id"] == r2["action_id"]
    # latest of kind=reorder is Reorder B
    assert pa.load_latest("shopping", kind="reorder")["id"] == r3["action_id"]


def test_load_latest_skips_expired(pa, workspace):
    r1 = pa.stage("shopping", "reorder", {"item": "fresh"}, "Fresh",
                  confirm_label="OK", cancel_label="No")
    # Inject an expired action that would otherwise be "newer" by staged_at
    file_path = pa._pending_path("shopping")
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)
    data["actions"].append({
        "id": "act_expired_recent",
        "kind": "reorder",
        "agent_id": "shopping",
        "batch_id": None,
        "staged_at": "2099-01-01T00:00:00+00:00",  # would beat r1
        "expires_at": "2020-01-01T04:00:00+00:00",  # but expired
        "summary": "Stale-but-future-staged",
        "confirm_label": "OK",
        "cancel_label": "No",
        "payload": {},
    })
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    latest = pa.load_latest("shopping")
    assert latest["id"] == r1["action_id"]


# ── execute ──────────────────────────────────────────────────────


def test_execute_runs_executor_and_removes_action(pa, workspace):
    result = pa.stage("shopping", "reorder", {"source": "costco"},
                      "Reorder X", confirm_label="OK", cancel_label="No")
    action = pa.load_by_id("shopping", result["action_id"])

    captured = {}
    def fake_executor(**kw):
        captured["kw"] = kw
        return {"status": "ok", "item": "X"}

    out = pa.execute("shopping", action, {"confirm_reorder": fake_executor})

    assert out["status"] == "ok"
    assert out["item"] == "X"
    assert captured["kw"] == {"source": "costco"}
    # removed
    assert pa.load_by_id("shopping", result["action_id"]) is None


def test_execute_missing_executor_returns_error_and_keeps_action(pa, workspace):
    result = pa.stage("shopping", "weird_kind", {"a": 1}, "Weird",
                      confirm_label="OK", cancel_label="No")
    action = pa.load_by_id("shopping", result["action_id"])

    out = pa.execute("shopping", action, {})

    assert out["status"] == "error"
    assert "weird_kind" in out["error"]
    # action should NOT be removed when no executor exists; the dispatcher
    # may want to surface a clearer error path before deciding what to do.
    # (existing _handle_confirm DOES remove in this case; the shared
    # helper preserves the action so the new confirm_pending tool
    # surface can let the LLM ask the operator what to do. Dispatcher-side
    # callers can remove explicitly if they prefer.)
    assert pa.load_by_id("shopping", result["action_id"]) is not None


def test_execute_executor_raises_returns_error_and_keeps_action(pa, workspace):
    result = pa.stage("shopping", "reorder", {"a": 1}, "Boom",
                      confirm_label="OK", cancel_label="No")
    action = pa.load_by_id("shopping", result["action_id"])

    def fake_executor(**kw):
        raise RuntimeError("kaboom")

    out = pa.execute("shopping", action, {"confirm_reorder": fake_executor})

    assert out["status"] == "error"
    assert "kaboom" in out["error"]
    # On exception, action stays — caller may want to retry.
    assert pa.load_by_id("shopping", result["action_id"]) is not None


def test_execute_non_dict_result_wraps_as_ok(pa, workspace):
    result = pa.stage("shopping", "reorder", {"a": 1}, "Plain",
                      confirm_label="OK", cancel_label="No")
    action = pa.load_by_id("shopping", result["action_id"])

    def fake_executor(**kw):
        return "all good"

    out = pa.execute("shopping", action, {"confirm_reorder": fake_executor})

    assert out["status"] == "ok"
    assert out["result"] == "all good"
    # removed on success
    assert pa.load_by_id("shopping", result["action_id"]) is None


def test_execute_dict_without_status_defaults_to_ok(pa, workspace):
    result = pa.stage("shopping", "reorder", {"a": 1}, "No status key",
                      confirm_label="OK", cancel_label="No")
    action = pa.load_by_id("shopping", result["action_id"])

    def fake_executor(**kw):
        return {"item": "Y"}

    out = pa.execute("shopping", action, {"confirm_reorder": fake_executor})

    assert out["status"] == "ok"
    assert out["item"] == "Y"
    assert pa.load_by_id("shopping", result["action_id"]) is None
