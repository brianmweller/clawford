"""Safeguard 2: source-cleanliness gate.

deploy.py must refuse to run if the manifest's source files are not a
clean git state (uncommitted modifications or untracked files). The
2026-04-12 incident would have been prevented: my local Clawford tree had
several uncommitted edits when I ran the deploy.

Override: --allow-dirty flag proceeds with a loud warning.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _make_args(agent_id="testagent", allow_dirty=False, dry_run=False):
    return argparse.Namespace(
        agent_id=agent_id,
        all=False,
        exclude=[],
        dry_run=dry_run,
        skip_files=False,
        skip_scripts=False,
        skip_crons=True,
        skip_channel=True,
        remove_orphans=False,
        allow_dirty=allow_dirty,
    )


def test_deploy_refuses_on_dirty_modified_source(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path
):
    """Modify a source file without committing; deploy.py must refuse."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    # Dirty the hello.py in the agent directory — uncommitted modification.
    soul = fake_source_repo / "agents" / "testagent" / "scripts" / "hello.py"
    soul.write_text(soul.read_text(encoding="utf-8") + "\nUNCOMMITTED EDIT\n", encoding="utf-8")
    # Verify git sees it dirty
    r = _run(["git", "status", "--porcelain"], cwd=fake_source_repo)
    assert "M agents/testagent/scripts/hello.py" in r.stdout or " M agents/testagent/scripts/hello.py" in r.stdout

    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc != 0, "deploy should refuse dirty source"
    # Workspace should be untouched (or contain only what was there before)
    assert not any(fake_workspace.iterdir()) or (fake_workspace / "scripts" / "hello.py").read_text(
        encoding="utf-8"
    ) != soul.read_text(encoding="utf-8"), "workspace must not have the uncommitted edit"


def test_deploy_refuses_on_untracked_source_file(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path
):
    """An untracked source file in the agent dir must also block."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    # Create a never-added file inside the agent's scripts dir
    untracked = fake_source_repo / "agents" / "testagent" / "scripts" / "sneaky.py"
    untracked.write_text("print('not tracked')\n", encoding="utf-8")
    # Verify git sees it
    r = _run(["git", "status", "--porcelain"], cwd=fake_source_repo)
    assert "sneaky.py" in r.stdout

    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc != 0, "deploy should refuse untracked source file"


def test_deploy_proceeds_when_source_clean(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path
):
    """Baseline: untouched committed repo deploys successfully."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc == 0, "clean source should deploy successfully"
    # Verify a file actually landed
    assert (fake_workspace / "scripts" / "hello.py").exists()


def test_deploy_proceeds_with_allow_dirty(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path
):
    """--allow-dirty overrides the gate (escape hatch for emergencies)."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    soul = fake_source_repo / "agents" / "testagent" / "scripts" / "hello.py"
    soul.write_text("dirty content\n", encoding="utf-8")

    rc = deploy_module.deploy_one("testagent", _make_args(allow_dirty=True))
    assert rc == 0, "--allow-dirty should permit a dirty-source deploy"
    assert (fake_workspace / "scripts" / "hello.py").read_text(encoding="utf-8") == "dirty content\n"
