"""Safeguard 3: diff preview + per-file confirmation on UPDATE.

Every file flagged UPDATE must show a unified diff and wait for user
approval before overwriting. Non-interactive runs require --yes-updates.
CREATE operations skip the gate (new files are always safe to add).
"""
from __future__ import annotations

import argparse
import io
import subprocess
from pathlib import Path

import pytest


def _make_args(agent_id="testagent", allow_dirty=False, yes_updates=False):
    return argparse.Namespace(
        agent_id=agent_id,
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
    )


def test_update_shows_diff_and_aborts_without_yes(
    deploy_module, fake_source_repo, prepopulated_workspace, monkeypatch,
    tmp_path, capsys,
):
    """An UPDATE is detected, diff is printed, user declines — file unchanged."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    # Mock input() to auto-decline ('n') every UPDATE prompt
    monkeypatch.setattr("builtins.input", lambda _="": "n")

    original_soul = (prepopulated_workspace / "SOUL.md").read_text(encoding="utf-8")
    assert "PRIOR" in original_soul

    rc = deploy_module.deploy_one("testagent", _make_args())
    # Auto-decline means deploy should report the file was kept as-is
    # (workspace file unchanged)
    assert (prepopulated_workspace / "SOUL.md").read_text(encoding="utf-8") == original_soul, (
        "declined UPDATE must not overwrite the target"
    )

    out = capsys.readouterr().out
    # Must show a diff marker
    assert "---" in out or "+++" in out or "diff" in out.lower() or "@@" in out, (
        f"no diff shown in output: {out[:400]}"
    )


def test_update_proceeds_with_yes_updates_flag(
    deploy_module, fake_source_repo, prepopulated_workspace, monkeypatch, tmp_path,
):
    """--yes-updates bypasses the prompt and applies the UPDATE."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    # input() should not be called at all; raise if it is
    def _raise(*a, **k):
        raise AssertionError("input() must not be called when --yes-updates is set")
    monkeypatch.setattr("builtins.input", _raise)

    rc = deploy_module.deploy_one("testagent", _make_args(yes_updates=True))
    assert rc == 0
    new_soul = (prepopulated_workspace / "SOUL.md").read_text(encoding="utf-8")
    assert "PRIOR" not in new_soul, "UPDATE should have replaced the prior content"


def test_create_proceeds_without_confirm(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path,
):
    """CREATE operations don't prompt. Fresh workspace deploys cleanly with
    no input() calls even without --yes-updates."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    def _raise(*a, **k):
        raise AssertionError("input() must not be called for CREATE-only deploy")
    monkeypatch.setattr("builtins.input", _raise)

    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc == 0
    assert (fake_workspace / "SOUL.md").exists()
