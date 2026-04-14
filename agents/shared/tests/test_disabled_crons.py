"""Tests for the Cron.enabled field and its effect on plan_cron_ops.

Phase 3b rolls news-digest's two OpenClaw LLM crons (morning-edition,
preference-update) off the cron dispatcher and onto host crons. The
manifest entries stay in place as rollback reference, marked with
`"enabled": false` — the plan's "disabled, not deleted" pattern.

Semantics:
  - `enabled` defaults to True (existing crons keep their behavior).
  - `enabled: false` excludes the cron from plan_cron_ops add/edit/skip
    logic. If the cron is still in live (i.e. not yet removed by
    `--remove-orphans`), it surfaces as an ORPHAN warning.
  - Cron hygiene check still runs on disabled crons — the manifest
    invariants should always hold in case rollback re-enables them.

Tests exercise load_manifest + plan_cron_ops directly against tmp
manifest JSON files and stub live-cron dicts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _minimal_manifest_dict(crons: list[dict]) -> dict:
    return {
        "agent_id": "testagent",
        "display_name": "Test",
        "workspace": "/tmp/test-ws",
        "status_file": "/tmp/test.status.md",
        "telegram": {"account": "testagent", "bot_token_env": "TEST_BOT_TOKEN"},
        "config_files": [],
        "scripts": [],
        "state_files": [],
        "approvals": {"allowlist": []},
        "crons": crons,
    }


def _write_manifest(tmp_path: Path, crons: list[dict]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_minimal_manifest_dict(crons)), encoding="utf-8")
    return path


def _live_cron(name: str, cron_id: str, message: str, expr: str) -> dict:
    return {
        "id": cron_id,
        "payload": {"message": message},
        "schedule": {"expr": expr},
    }


# ─── load_manifest: enabled field ───────────────────────────────────


def test_load_manifest_enabled_defaults_to_true(deploy_module, tmp_path):
    path = _write_manifest(tmp_path, [{
        "name": "morning",
        "cron": "0 10 * * *",
        "message": "run the thing",
    }])
    mf = deploy_module.load_manifest(path)
    assert mf.crons[0].enabled is True


def test_load_manifest_reads_enabled_false(deploy_module, tmp_path):
    path = _write_manifest(tmp_path, [{
        "name": "morning",
        "cron": "0 10 * * *",
        "message": "run the thing",
        "enabled": False,
    }])
    mf = deploy_module.load_manifest(path)
    assert mf.crons[0].enabled is False


def test_load_manifest_reads_enabled_true_explicit(deploy_module, tmp_path):
    path = _write_manifest(tmp_path, [{
        "name": "morning",
        "cron": "0 10 * * *",
        "message": "run the thing",
        "enabled": True,
    }])
    mf = deploy_module.load_manifest(path)
    assert mf.crons[0].enabled is True


# ─── plan_cron_ops: enabled=True (baseline behavior preserved) ──────


def test_plan_cron_ops_adds_new_enabled_cron(deploy_module, tmp_path):
    path = _write_manifest(tmp_path, [{
        "name": "morning",
        "cron": "0 10 * * *",
        "message": "run the thing",
    }])
    mf = deploy_module.load_manifest(path)

    live: dict = {}
    ops = deploy_module.plan_cron_ops(mf, live, "chat123")

    assert len(ops) == 1
    assert ops[0]["op"] == "add"
    assert ops[0]["spec"]["name"] == "morning"


def test_plan_cron_ops_skips_matching_enabled_cron(deploy_module, tmp_path):
    path = _write_manifest(tmp_path, [{
        "name": "morning",
        "cron": "0 10 * * *",
        "message": "run the thing",
    }])
    mf = deploy_module.load_manifest(path)

    live = {"morning": _live_cron("morning", "j1", "run the thing", "0 10 * * *")}
    ops = deploy_module.plan_cron_ops(mf, live, "chat123")

    assert len(ops) == 1
    assert ops[0]["op"] == "skip"


# ─── plan_cron_ops: enabled=False behavior ──────────────────────────


def test_plan_cron_ops_disabled_cron_not_added_when_absent(
    deploy_module, tmp_path
):
    """A disabled cron must NOT be added to live — even if it's not
    currently there. `enabled: false` is a promise that the operator
    doesn't want OpenClaw running it."""
    path = _write_manifest(tmp_path, [{
        "name": "morning",
        "cron": "0 10 * * *",
        "message": "run the thing",
        "enabled": False,
    }])
    mf = deploy_module.load_manifest(path)

    live: dict = {}
    ops = deploy_module.plan_cron_ops(mf, live, "chat123")

    # No op should target the disabled cron
    assert all(op["op"] != "add" for op in ops), f"unexpected add op: {ops}"
    disabled_touched = any(
        op.get("name") == "morning" or op.get("spec", {}).get("name") == "morning"
        for op in ops
    )
    assert not disabled_touched


def test_plan_cron_ops_disabled_cron_surfaces_as_orphan_when_live(
    deploy_module, tmp_path
):
    """When the disabled cron is still in live (e.g. not yet removed),
    plan_cron_ops should emit an orphan op so --remove-orphans can
    clean it up."""
    path = _write_manifest(tmp_path, [{
        "name": "morning",
        "cron": "0 10 * * *",
        "message": "run the thing",
        "enabled": False,
    }])
    mf = deploy_module.load_manifest(path)

    live = {"morning": _live_cron("morning", "j1", "run the thing", "0 10 * * *")}
    ops = deploy_module.plan_cron_ops(mf, live, "chat123")

    # The disabled cron should be reported as orphan
    orphan_ops = [op for op in ops if op["op"] == "orphan"]
    assert len(orphan_ops) == 1
    assert orphan_ops[0]["name"] == "morning"
    assert orphan_ops[0]["id"] == "j1"

    # And it should NOT be skipped/edited/added
    for op in ops:
        if op["op"] in ("add", "edit", "skip"):
            name = op.get("name") or op.get("spec", {}).get("name")
            assert name != "morning", f"disabled cron got {op['op']}: {op}"


def test_plan_cron_ops_mixed_enabled_and_disabled(deploy_module, tmp_path):
    """A manifest with one enabled + one disabled cron should emit
    normal ops for the enabled one and orphan the disabled one."""
    path = _write_manifest(tmp_path, [
        {
            "name": "active",
            "cron": "*/5 * * * *",
            "message": "keepalive",
        },
        {
            "name": "retired",
            "cron": "0 10 * * *",
            "message": "old morning thing",
            "enabled": False,
        },
    ])
    mf = deploy_module.load_manifest(path)

    live = {
        "active":  _live_cron("active",  "j1", "keepalive",      "*/5 * * * *"),
        "retired": _live_cron("retired", "j2", "old morning thing", "0 10 * * *"),
    }
    ops = deploy_module.plan_cron_ops(mf, live, "chat123")

    by_name = {}
    for op in ops:
        name = op.get("name") or op.get("spec", {}).get("name")
        by_name[name] = op

    assert by_name["active"]["op"] == "skip"
    assert by_name["retired"]["op"] == "orphan"


# ─── Cron hygiene still applies to disabled crons ───────────────────


def test_cron_hygiene_still_runs_on_disabled_crons(deploy_module, tmp_path):
    """Rollback risk: if you disable a cron with a hygiene violation
    and later re-enable it, you'd ship the violation. Keep hygiene
    enforced regardless of enabled."""
    path = _write_manifest(tmp_path, [{
        "name": "morning",
        "cron": "0 10 * * *",
        "message": "python3 foo.py ; echo $?",  # forbidden shell operator
        "enabled": False,
    }])
    mf = deploy_module.load_manifest(path)

    errors = deploy_module.check_cron_message_hygiene(mf)
    assert errors, "hygiene check should still fail on disabled cron"
