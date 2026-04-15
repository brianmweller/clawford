"""Drift detection must not flag state_files as drift.

State files are owned by the live agent at runtime (cron runs write to
them continuously), so they must be excluded from the drift check. Two
legs:

  1. Forward behavior: a fresh deploy followed by a state-file mutation
     followed by a re-deploy should NOT report a drift violation.

  2. Backward compat: a pre-existing drift manifest written by an older
     deploy.py (before this fix) may still contain state_file entries.
     The check must skip them rather than flagging them as DELETED.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest


def _make_args(yes_updates=True, accept_drift=False):
    return argparse.Namespace(
        agent_id="stateagent",
        all=False,
        exclude=[],
        dry_run=False,
        skip_files=False,
        skip_scripts=False,
        skip_crons=True,
        skip_channel=True,
        remove_orphans=False,
        allow_dirty=False,
        yes_updates=yes_updates,
        accept_drift=accept_drift,
    )


def _run(cmd, cwd):
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        f"{' '.join(cmd)} failed: {result.stderr or result.stdout}"
    )
    return result


@pytest.fixture
def source_repo_with_state_file(tmp_path: Path) -> Path:
    """A source repo whose manifest declares a state file."""
    repo = tmp_path / "source-repo"
    agent_dir = repo / "agents" / "stateagent"
    (agent_dir / "scripts").mkdir(parents=True)
    (agent_dir / "SOUL.md").write_text("# stateagent soul\n", encoding="utf-8")
    (agent_dir / "IDENTITY.md").write_text("# stateagent identity\n", encoding="utf-8")
    (agent_dir / "scripts" / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (agent_dir / "scripts" / "heartbeat.py").write_text(
        "print('{\"status\": \"ok\"}')\n", encoding="utf-8"
    )

    manifest = {
        "agent_id": "stateagent",
        "display_name": "State Agent",
        "workspace": str(tmp_path / "state-workspace"),
        "status_file": str(tmp_path / "fake-brain" / "stateagent.status.md"),
        "telegram": {"account": "stateagent", "bot_token_env": "TEST_BOT_TOKEN"},
        "config_files": [
            {"src": "SOUL.md", "immutable": True},
            {"src": "IDENTITY.md", "immutable": True},
        ],
        "scripts": ["scripts/hello.py", "scripts/heartbeat.py"],
        "state_files": [
            {
                "path": "pending-actions.json",
                "seed_if_absent": {"updated_at": None, "actions": []},
            }
        ],
        "approvals": {"allowlist": []},
        "crons": [],
    }
    (agent_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    ops_dir = repo / "ops"
    ops_dir.mkdir(exist_ok=True)
    baseline = {
        "defaults": {"security": "full", "ask": "off"},
        "agents": {
            "main":       {"security": "full", "policy": "full", "ask": "off"},
            "stateagent": {"security": "full", "policy": "full", "ask": "off"},
        },
    }
    (ops_dir / "exec-approvals-baseline.json").write_text(
        json.dumps(baseline, indent=2), encoding="utf-8"
    )

    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "test"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "initial"], cwd=repo)
    return repo


@pytest.fixture
def deploy_module_stateagent(source_repo_with_state_file, monkeypatch):
    import sys
    for mod in list(sys.modules):
        if mod == "deploy" or mod.startswith("deploy."):
            del sys.modules[mod]
    SHARED_DIR = Path(__file__).resolve().parent.parent
    if str(SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(SHARED_DIR))
    import deploy  # type: ignore
    monkeypatch.setattr(deploy, "REPO_ROOT", source_repo_with_state_file)

    def fake_oc(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(deploy, "oc", fake_oc)

    def fake_oc_json(*args, **kwargs):
        if args[:2] == ("config", "validate"):
            return {"valid": True, "path": "/fake/openclaw.json"}
        if args[:2] == ("approvals", "get"):
            return {
                "defaults": {"security": "full", "ask": "off"},
                "agents": {
                    "main":       {"security": "full", "policy": "full", "ask": "off", "allowlist": []},
                    "stateagent": {"security": "full", "policy": "full", "ask": "off", "allowlist": []},
                },
            }
        return {"jobs": []}
    monkeypatch.setattr(deploy, "oc_json", fake_oc_json)
    return deploy


def test_state_file_mutation_is_not_drift(
    deploy_module_stateagent, tmp_path, monkeypatch, capsys
):
    """After a first deploy, mutating the declared state file should NOT
    cause the next deploy to report a drift violation."""
    monkeypatch.setattr(
        deploy_module_stateagent, "BACKUPS_ROOT",
        tmp_path / "backups", raising=False,
    )

    rc1 = deploy_module_stateagent.deploy_one("stateagent", _make_args())
    assert rc1 == 0, "first deploy should succeed"

    workspace = tmp_path / "state-workspace"
    state_path = workspace / "pending-actions.json"
    assert state_path.exists(), "seed_if_absent should have created the state file"

    # Simulate a cron run writing new content.
    state_path.write_text(
        json.dumps({"updated_at": "2026-04-12", "actions": [{"id": 1}]}),
        encoding="utf-8",
    )

    capsys.readouterr()
    rc2 = deploy_module_stateagent.deploy_one("stateagent", _make_args())
    out = capsys.readouterr().out
    assert "DRIFT VIOLATION" not in out, (
        f"state file mutation must not trigger drift: {out[:500]}"
    )
    assert rc2 == 0, "second deploy should succeed silently"


def test_check_drift_ignores_state_files_in_legacy_manifest(
    deploy_module_stateagent, tmp_path, monkeypatch
):
    """A drift manifest written by older deploy.py (that tracked state
    files) must not cause DRIFT on current deploy.py runs. We emulate
    this by writing a legacy manifest by hand and calling check_drift
    directly."""
    monkeypatch.setattr(
        deploy_module_stateagent, "BACKUPS_ROOT",
        tmp_path / "backups", raising=False,
    )

    # First deploy populates the workspace.
    rc = deploy_module_stateagent.deploy_one("stateagent", _make_args())
    assert rc == 0

    # Load the manifest via the normal loader.
    manifest_path = Path(deploy_module_stateagent.REPO_ROOT) / "agents" / "stateagent" / "manifest.json"
    mf = deploy_module_stateagent.load_manifest(manifest_path)

    # Hand-write a legacy drift manifest that includes the state file.
    legacy = {
        "agent_id": "stateagent",
        "deploy_ts": "2026-04-01T00:00:00Z",
        "files": {
            "SOUL.md": deploy_module_stateagent._sha256(mf.expanded_workspace / "SOUL.md"),
            "scripts/hello.py": deploy_module_stateagent._sha256(mf.expanded_workspace / "scripts/hello.py"),
            # Legacy entry: a state file with a stale hash from the past.
            "pending-actions.json": "deadbeef" * 8,
        },
    }
    drift_path = deploy_module_stateagent._drift_manifest_path("stateagent")
    drift_path.parent.mkdir(parents=True, exist_ok=True)
    drift_path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    # Mutate the state file so it differs from the stale legacy hash.
    (mf.expanded_workspace / "pending-actions.json").write_text(
        json.dumps({"updated_at": "2026-04-12", "actions": []}),
        encoding="utf-8",
    )

    drifted = deploy_module_stateagent.check_drift(mf)
    state_paths = {path for (path, _, _) in drifted}
    assert "pending-actions.json" not in state_paths, (
        f"state_file must be filtered from legacy drift manifest; got: {drifted}"
    )
