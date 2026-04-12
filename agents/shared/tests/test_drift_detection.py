"""Safeguard 4: VPS drift detection — BLOCKING.

After a successful deploy, deploy.py writes a manifest of the workspace
state (per-file sha256). On subsequent deploys, if any file in the
workspace has drifted from the recorded state, refuse to proceed unless
--accept-drift is passed. This enforces the NO ON-VPS DEV rule — any
direct edit to the workspace between deploys triggers a blocking violation.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest


def _make_args(yes_updates=True, accept_drift=False, allow_dirty=False):
    return argparse.Namespace(
        agent_id="testagent",
        all=False,
        exclude=[],
        dry_run=False,
        skip_files=False,
        skip_scripts=False,
        skip_crons=True,
        skip_channel=True,
        remove_orphans=False,
        allow_dirty=allow_dirty,
        yes_updates=yes_updates,
        accept_drift=accept_drift,
    )


def test_drift_silent_on_first_deploy(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path,
):
    """Very first deploy into an empty workspace: no prior manifest, no
    drift check, no warning."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc == 0


def test_drift_silent_on_clean_rerun(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys,
):
    """Deploy twice in a row without touching the workspace in between:
    drift check passes silently (no DRIFT VIOLATION message)."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    rc1 = deploy_module.deploy_one("testagent", _make_args())
    assert rc1 == 0
    capsys.readouterr()  # clear
    rc2 = deploy_module.deploy_one("testagent", _make_args())
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "DRIFT VIOLATION" not in out2, f"unexpected drift on clean rerun: {out2[:300]}"


def test_drift_blocks_on_workspace_mutation_between_deploys(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys,
):
    """Deploy, hand-edit a workspace file, redeploy: second deploy must
    refuse with a DRIFT VIOLATION error."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    # First deploy establishes the baseline
    rc1 = deploy_module.deploy_one("testagent", _make_args())
    assert rc1 == 0

    # Simulate direct VPS-side editing
    rogue = fake_workspace / "SOUL.md"
    rogue.write_text("# testagent soul ROGUE EDIT ON VPS\n", encoding="utf-8")

    capsys.readouterr()
    rc2 = deploy_module.deploy_one("testagent", _make_args())
    assert rc2 != 0, "deploy must refuse when workspace has drifted"
    out = capsys.readouterr().out
    assert "DRIFT" in out, f"no DRIFT warning in output: {out[:400]}"


def test_drift_allows_accept_drift_flag(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys,
):
    """--accept-drift overrides the block and proceeds. Rogue edit is
    overwritten by the source content."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    rc1 = deploy_module.deploy_one("testagent", _make_args())
    assert rc1 == 0

    rogue = fake_workspace / "SOUL.md"
    rogue.write_text("# testagent soul ROGUE\n", encoding="utf-8")

    rc2 = deploy_module.deploy_one("testagent", _make_args(accept_drift=True))
    assert rc2 == 0
    # After applying with accept-drift, the source content should be back
    assert "# testagent soul\n" == (fake_workspace / "SOUL.md").read_text(encoding="utf-8")
