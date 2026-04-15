"""Phase 5 (liberation): deploy.py Safeguards rewrite — aggregate test file.

This file replaces the narrower test_config_validate_guard.py (Safeguard 7)
and test_smoke_test.py (Safeguard 6), and absorbs the deletion of
test_exec_approvals_baseline_guard.py (Safeguard 8, gone).

Headline acceptance criterion: `deploy.py --dry-run` must not invoke oc()
at any point. The test monkeypatches every oc-adjacent helper to raise and
runs deploy_one() against a fixture agent with dry_run=True.

Safeguard 7 was "validate OpenClaw gateway config" — now it's
"validate the per-agent manifest.json the user is about to deploy."
Safeguard 6 was "fire an OpenClaw cron and poll for status" — now it's
"run a host subprocess against the agent workspace and assert exit 0 +
non-empty stdout."
Safeguard 8 is gone entirely; this file asserts it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"


def _make_args(
    dry_run=True,
    smoke_test=False,
    skip_crons=False,
    skip_channel=True,
    skip_files=False,
    skip_scripts=False,
):
    return argparse.Namespace(
        agent_id="testagent",
        all=False,
        exclude=[],
        dry_run=dry_run,
        skip_files=skip_files,
        skip_scripts=skip_scripts,
        skip_crons=skip_crons,
        skip_channel=skip_channel,
        remove_orphans=False,
        remove_orphan_scripts=False,
        allow_dirty=False,
        yes_updates=True,
        accept_drift=True,
        smoke_test=smoke_test,
    )


# ---------------------------------------------------------------------------
# Headline test: dry-run must not touch oc() at all
# ---------------------------------------------------------------------------


def test_dry_run_invokes_no_oc(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path,
):
    """Monkeypatch every oc-adjacent helper to raise. deploy_one with
    dry_run=True must complete without triggering any of them.

    This is the Phase 5 acceptance criterion — the bridge between the
    liberation effort and Phase 6's OpenClaw decommission. Once this test
    is green, dry-run is structurally decoupled from OpenClaw.
    """
    def raiser(*args, **kwargs):
        raise AssertionError(
            f"oc-path was called from dry-run: args={args} kwargs={kwargs}"
        )

    monkeypatch.setattr(deploy_module, "oc", raiser)
    monkeypatch.setattr(deploy_module, "oc_json", raiser)
    monkeypatch.setattr(deploy_module, "oc_cron_add", raiser)
    monkeypatch.setattr(deploy_module, "oc_cron_rm", raiser)
    monkeypatch.setattr(deploy_module, "oc_cron_edit_message", raiser)
    monkeypatch.setattr(deploy_module, "fetch_live_crons", raiser)

    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    monkeypatch.setattr(deploy_module, "_DRY", True, raising=False)

    rc = deploy_module.deploy_one(
        "testagent",
        _make_args(dry_run=True, skip_crons=False, skip_channel=True),
    )
    assert rc == 0, f"dry-run deploy returned {rc}, expected 0"


# ---------------------------------------------------------------------------
# Phase 6: live-run must also not touch oc() — the stronger invariant
# ---------------------------------------------------------------------------


def test_live_run_invokes_no_oc(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path,
):
    """Phase 6 acceptance: a LIVE deploy (not dry-run) must not invoke any
    oc-adjacent helper. This is strictly stronger than the Phase 5 dry-run
    invariant.

    Post-Phase 6, main() no longer calls ensure_channel / ensure_binding /
    ensure_approvals or fetch_live_crons / plan_cron_ops / apply_cron_ops.
    The helper function *bodies* still exist as dead code (Phase 7 deletes
    them), but deploy_one has no path that reaches them.
    """
    def raiser(*args, **kwargs):
        raise AssertionError(
            f"oc-path was called from live-run: args={args} kwargs={kwargs}"
        )

    monkeypatch.setattr(deploy_module, "oc", raiser)
    monkeypatch.setattr(deploy_module, "oc_json", raiser)
    monkeypatch.setattr(deploy_module, "oc_cron_add", raiser)
    monkeypatch.setattr(deploy_module, "oc_cron_rm", raiser)
    monkeypatch.setattr(deploy_module, "oc_cron_edit_message", raiser)
    monkeypatch.setattr(deploy_module, "fetch_live_crons", raiser)
    monkeypatch.setattr(deploy_module, "plan_cron_ops", raiser)
    monkeypatch.setattr(deploy_module, "apply_cron_ops", raiser)
    monkeypatch.setattr(deploy_module, "ensure_channel", raiser)
    monkeypatch.setattr(deploy_module, "ensure_binding", raiser)
    monkeypatch.setattr(deploy_module, "ensure_approvals", raiser)

    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    monkeypatch.setattr(deploy_module, "_DRY", False, raising=False)

    rc = deploy_module.deploy_one(
        "testagent",
        _make_args(
            dry_run=False,
            skip_files=True,
            skip_crons=False,
            skip_channel=False,
        ),
    )
    assert rc == 0, f"live-run deploy returned {rc}, expected 0"


def test_live_run_skips_channel_binding_approvals(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path,
):
    """Live deploy must NOT call ensure_channel / ensure_binding /
    ensure_approvals. Pre-Phase 6 those were invoked unconditionally from
    main() at lines 1657-1661; Phase 6 removes that block."""
    def raiser(*args, **kwargs):
        raise AssertionError(
            f"channel/binding/approvals helper was called from live-run: "
            f"args={args} kwargs={kwargs}"
        )

    monkeypatch.setattr(deploy_module, "ensure_channel", raiser)
    monkeypatch.setattr(deploy_module, "ensure_binding", raiser)
    monkeypatch.setattr(deploy_module, "ensure_approvals", raiser)

    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    monkeypatch.setattr(deploy_module, "_DRY", False, raising=False)

    rc = deploy_module.deploy_one(
        "testagent",
        _make_args(
            dry_run=False,
            skip_files=True,
            skip_crons=False,
            skip_channel=False,
        ),
    )
    assert rc == 0, f"live-run deploy returned {rc}, expected 0"


def test_live_run_skips_cron_reconciliation(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path,
):
    """Live deploy must NOT call fetch_live_crons / plan_cron_ops /
    apply_cron_ops. Pre-Phase 6 these were invoked from main()'s else
    branch at lines 1673-1682; Phase 6 removes that block so crons live
    exclusively under ops/scripts/install-host-cron.sh."""
    def raiser(*args, **kwargs):
        raise AssertionError(
            f"cron-reconciliation helper was called from live-run: "
            f"args={args} kwargs={kwargs}"
        )

    monkeypatch.setattr(deploy_module, "fetch_live_crons", raiser)
    monkeypatch.setattr(deploy_module, "plan_cron_ops", raiser)
    monkeypatch.setattr(deploy_module, "apply_cron_ops", raiser)

    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    monkeypatch.setattr(deploy_module, "_DRY", False, raising=False)

    rc = deploy_module.deploy_one(
        "testagent",
        _make_args(
            dry_run=False,
            skip_files=True,
            skip_crons=False,
            skip_channel=True,
        ),
    )
    assert rc == 0, f"live-run deploy returned {rc}, expected 0"


# ---------------------------------------------------------------------------
# Safeguard 7: validate_manifest
# ---------------------------------------------------------------------------


def _real_agent_manifest_examples():
    """Yield (agent_id, manifest_path) for every agent with a
    manifest.json.example committed in the repo."""
    for agent_dir in sorted(AGENTS_DIR.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name == "shared":
            continue
        example = agent_dir / "manifest.json.example"
        if example.exists():
            yield agent_dir.name, example


@pytest.mark.parametrize(
    "agent_id,example_path",
    list(_real_agent_manifest_examples()),
    ids=lambda x: x if isinstance(x, str) else x.name,
)
def test_validate_manifest_clean_real_agents(
    deploy_module, agent_id, example_path,
):
    """Every real agent's manifest.json.example must pass validate_manifest.

    Any violation flagged here is a bug in the validator, not the manifest.
    These files are the ground truth for what shape Clawford manifests take.
    """
    with open(example_path, encoding="utf-8") as f:
        data = json.load(f)

    mf = deploy_module.load_manifest_from_dict(data)
    errors = deploy_module.validate_manifest(mf, expected_agent_id=agent_id)
    assert errors == [], (
        f"validate_manifest rejected real agent {agent_id}:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


def test_validate_manifest_duplicate_cron_name(deploy_module):
    """Two crons with the same name → violation mentioning the name."""
    mf = deploy_module.load_manifest_from_dict(
        _synthetic_manifest(
            crons=[
                {"name": "morning", "cron": "0 10 * * *", "message": "x"},
                {"name": "morning", "cron": "0 20 * * *", "message": "y"},
            ]
        )
    )
    errors = deploy_module.validate_manifest(mf)
    assert any("morning" in e and "duplicate" in e.lower() for e in errors), errors


def test_validate_manifest_missing_soul_md(deploy_module):
    """config_files must include SOUL.md (the agent's identity anchor)."""
    mf = deploy_module.load_manifest_from_dict(
        _synthetic_manifest(config_files=[{"src": "IDENTITY.md", "immutable": True}])
    )
    errors = deploy_module.validate_manifest(mf)
    assert any("SOUL.md" in e for e in errors), errors


def test_validate_manifest_missing_identity_md(deploy_module):
    """config_files must include IDENTITY.md (deploy-time PII placeholder gate)."""
    mf = deploy_module.load_manifest_from_dict(
        _synthetic_manifest(config_files=[{"src": "SOUL.md", "immutable": True}])
    )
    errors = deploy_module.validate_manifest(mf)
    assert any("IDENTITY.md" in e for e in errors), errors


def test_validate_manifest_scripts_missing_heartbeat(deploy_module):
    """scripts list must contain scripts/heartbeat.py — it's the fleet-health
    probe every agent is required to ship."""
    mf = deploy_module.load_manifest_from_dict(
        _synthetic_manifest(scripts=["scripts/other.py"])
    )
    errors = deploy_module.validate_manifest(mf)
    assert any("heartbeat" in e.lower() for e in errors), errors


def test_validate_manifest_smoke_script_not_in_scripts_list(deploy_module):
    """smoke_test.script must reference a file in the scripts list."""
    mf = deploy_module.load_manifest_from_dict(
        _synthetic_manifest(
            scripts=["scripts/heartbeat.py"],
            smoke_test={"script": "scripts/ghost.py", "max_wait_s": 30},
        )
    )
    errors = deploy_module.validate_manifest(mf)
    assert any("ghost.py" in e for e in errors), errors


def test_validate_manifest_absolute_state_file_path(deploy_module):
    """state_files[].path must be relative — an absolute or ~-prefixed path
    would point outside the workspace."""
    mf = deploy_module.load_manifest_from_dict(
        _synthetic_manifest(
            state_files=[{"path": "/etc/passwd", "seed_if_absent": {}}],
        )
    )
    errors = deploy_module.validate_manifest(mf)
    assert any("/etc/passwd" in e or "absolute" in e.lower() for e in errors), errors


def test_validate_manifest_agent_id_mismatch(deploy_module):
    """manifest agent_id must match the agent dir name being deployed."""
    mf = deploy_module.load_manifest_from_dict(
        _synthetic_manifest(agent_id="renamed-agent")
    )
    errors = deploy_module.validate_manifest(mf, expected_agent_id="testagent")
    assert any("agent_id" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Safeguard 6: run_smoke_test — host subprocess, not oc cron run
# ---------------------------------------------------------------------------


def _dummy_manifest(deploy_module, tmp_path: Path, smoke_test: dict | None):
    """Build a Manifest pointing at a tmp_path workspace with a fake
    heartbeat script present, so run_smoke_test has something to resolve."""
    ws = tmp_path / "dummy-workspace"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "heartbeat.py").write_text(
        "print('{\"status\": \"ok\"}')\n", encoding="utf-8"
    )
    return deploy_module.load_manifest_from_dict(
        _synthetic_manifest(
            workspace=str(ws),
            smoke_test=smoke_test,
        )
    )


def test_run_smoke_test_success(deploy_module, tmp_path, monkeypatch):
    """Exit 0 + non-empty stdout → True."""
    mf = _dummy_manifest(
        deploy_module, tmp_path,
        {"script": "scripts/heartbeat.py", "max_wait_s": 30},
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"status": "ok"}\n', stderr=""
        )
    monkeypatch.setattr(deploy_module.subprocess, "run", fake_run)

    ok = deploy_module.run_smoke_test(mf, mf.smoke_test)
    assert ok is True


def test_run_smoke_test_failure_returncode(deploy_module, tmp_path, monkeypatch):
    """Non-zero exit → False."""
    mf = _dummy_manifest(
        deploy_module, tmp_path,
        {"script": "scripts/heartbeat.py", "max_wait_s": 30},
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout='{"status": "error"}\n', stderr="boom"
        )
    monkeypatch.setattr(deploy_module.subprocess, "run", fake_run)

    ok = deploy_module.run_smoke_test(mf, mf.smoke_test)
    assert ok is False


def test_run_smoke_test_failure_empty_stdout(deploy_module, tmp_path, monkeypatch):
    """Exit 0 but empty stdout → False. Enforces the script contract: every
    script emits at least one JSON line on stdout."""
    mf = _dummy_manifest(
        deploy_module, tmp_path,
        {"script": "scripts/heartbeat.py", "max_wait_s": 30},
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )
    monkeypatch.setattr(deploy_module.subprocess, "run", fake_run)

    ok = deploy_module.run_smoke_test(mf, mf.smoke_test)
    assert ok is False


def test_run_smoke_test_failure_timeout(deploy_module, tmp_path, monkeypatch):
    """subprocess.TimeoutExpired → False."""
    mf = _dummy_manifest(
        deploy_module, tmp_path,
        {"script": "scripts/heartbeat.py", "max_wait_s": 1},
    )

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
    monkeypatch.setattr(deploy_module.subprocess, "run", fake_run)

    ok = deploy_module.run_smoke_test(mf, mf.smoke_test)
    assert ok is False


def test_run_smoke_test_backward_compat_cron_name_key(
    deploy_module, tmp_path, monkeypatch,
):
    """Old manifest shape {cron_name, max_wait_s} must still work — it falls
    through to scripts/heartbeat.py, the universal default.

    This lets real gitignored manifest.json copies on the VPS keep working
    without a blocking edit across all 6 agents.
    """
    mf = _dummy_manifest(
        deploy_module, tmp_path,
        {"cron_name": "heartbeat", "max_wait_s": 60},
    )

    captured_cmd = []
    def fake_run(cmd, **kwargs):
        captured_cmd.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"status": "ok"}\n', stderr=""
        )
    monkeypatch.setattr(deploy_module.subprocess, "run", fake_run)

    ok = deploy_module.run_smoke_test(mf, mf.smoke_test)
    assert ok is True
    assert captured_cmd, "fake subprocess.run was never called"
    assert any("heartbeat.py" in arg for arg in captured_cmd[0]), captured_cmd[0]


# ---------------------------------------------------------------------------
# Safeguard 8 — structural assertion that it's gone
# ---------------------------------------------------------------------------


def test_safeguard_8_check_function_removed(deploy_module):
    """check_exec_approvals_baseline was deleted in Phase 5 of liberation.
    Assert the symbol is not present. Catches accidental resurrection."""
    assert not hasattr(deploy_module, "check_exec_approvals_baseline"), (
        "check_exec_approvals_baseline should have been deleted in Phase 5"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _synthetic_manifest(
    *,
    agent_id="testagent",
    display_name="Test Agent",
    workspace=None,
    config_files=None,
    scripts=None,
    state_files=None,
    crons=None,
    smoke_test=None,
) -> dict:
    """Build a minimal-but-valid manifest dict, overriding specific fields."""
    return {
        "agent_id": agent_id,
        "display_name": display_name,
        "workspace": workspace or "/tmp/test-workspace",
        "status_file": "/tmp/fake-brain/testagent.status.md",
        "telegram": {"account": "testagent", "bot_token_env": "TEST_BOT_TOKEN"},
        "config_files": (
            config_files
            if config_files is not None
            else [
                {"src": "SOUL.md", "immutable": True},
                {"src": "IDENTITY.md", "immutable": True},
                {"src": "TOOLS.md"},
            ]
        ),
        "scripts": scripts if scripts is not None else ["scripts/heartbeat.py"],
        "state_files": state_files if state_files is not None else [],
        "approvals": {"allowlist": []},
        "crons": crons if crons is not None else [],
        **({"smoke_test": smoke_test} if smoke_test is not None else {}),
    }
