"""Tests for sync_scripts orphan-script removal.

When a script is deleted from the manifest (e.g. Phase 3b's
deliver-digest.py removal), sync_scripts previously left the file
sitting in the workspace as dead bytes — harmless but confusing,
and a trap for "why is this old script still running?" questions.

The --remove-orphan-scripts flag teaches sync_scripts to delete any
*.py file under <workspace>/scripts/ that is not in the manifest's
scripts list. Off by default so existing deploys don't suddenly
sweep away files a second operator placed by hand.

These tests exercise the new behavior against the conftest fake
repo plus a workspace seeded with an "orphan" script.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest


def _make_args(*, remove_orphan_scripts: bool = False):
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
        remove_orphan_scripts=remove_orphan_scripts,
        allow_dirty=False,
        yes_updates=True,
    )


# ─── sync_scripts orphan removal (standalone helper path) ───────────


def test_sync_scripts_without_flag_preserves_orphans(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch
):
    """Default behavior: leave orphan .py files in place. This is the
    pre-Phase-3b-followup safety baseline."""
    # Run a normal deploy first so the workspace has the manifest-listed scripts
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", fake_workspace.parent / "backups", raising=False)

    mf = deploy_module.load_manifest(
        fake_source_repo / "agents" / "testagent" / "manifest.json"
    )

    # Seed an orphan script in the workspace that isn't in the manifest
    scripts_dir = fake_workspace / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    orphan = scripts_dir / "legacy-goodbye.py"
    orphan.write_text("print('i should survive the default run')\n", encoding="utf-8")

    deploy_module.sync_scripts(mf, yes_updates=True)

    assert orphan.exists(), "default sync_scripts must not delete orphans"


def test_sync_scripts_with_flag_removes_py_orphans(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch
):
    """Opt-in behavior: --remove-orphan-scripts sweeps workspace scripts
    that are no longer in the manifest's scripts list."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", fake_workspace.parent / "backups", raising=False)

    mf = deploy_module.load_manifest(
        fake_source_repo / "agents" / "testagent" / "manifest.json"
    )

    scripts_dir = fake_workspace / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    # The manifest lists scripts/hello.py; seed an unrelated .py orphan
    orphan = scripts_dir / "deliver-digest.py"
    orphan.write_text("print('legacy, should be swept')\n", encoding="utf-8")

    deploy_module.sync_scripts(mf, yes_updates=True, remove_orphan_scripts=True)

    assert not orphan.exists(), "orphan .py must be deleted under --remove-orphan-scripts"


def test_sync_scripts_orphan_flag_preserves_manifest_listed_scripts(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch
):
    """Sanity gate: the sweep must not delete the scripts the manifest
    actually lists."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", fake_workspace.parent / "backups", raising=False)

    mf = deploy_module.load_manifest(
        fake_source_repo / "agents" / "testagent" / "manifest.json"
    )

    # Normal deploy populates scripts/hello.py
    deploy_module.sync_scripts(mf, yes_updates=True)
    assert (fake_workspace / "scripts" / "hello.py").exists()

    # Second run with the flag — should NOT nuke hello.py
    deploy_module.sync_scripts(mf, yes_updates=True, remove_orphan_scripts=True)
    assert (fake_workspace / "scripts" / "hello.py").exists()


def test_sync_scripts_orphan_flag_only_targets_py_files(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch
):
    """Non-.py files in scripts/ (e.g. .js extractors, .json seeds) are
    untouched by the sweep. Only Python files are swept so the blast
    radius is predictable."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", fake_workspace.parent / "backups", raising=False)

    mf = deploy_module.load_manifest(
        fake_source_repo / "agents" / "testagent" / "manifest.json"
    )

    scripts_dir = fake_workspace / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Orphan .py gets swept, orphan .js / .json stay
    (scripts_dir / "orphan.py").write_text("# orphan\n", encoding="utf-8")
    (scripts_dir / "helper.js").write_text("// not python\n", encoding="utf-8")
    (scripts_dir / "seeds.json").write_text("{}\n", encoding="utf-8")

    deploy_module.sync_scripts(mf, yes_updates=True, remove_orphan_scripts=True)

    assert not (scripts_dir / "orphan.py").exists()
    assert (scripts_dir / "helper.js").exists()
    assert (scripts_dir / "seeds.json").exists()


def test_sync_scripts_orphan_flag_respects_dry_run(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch
):
    """Under _DRY, the sweep logs the planned removal but doesn't
    actually unlink. Same discipline as every other sync_* helper."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", fake_workspace.parent / "backups", raising=False)
    monkeypatch.setattr(deploy_module, "_DRY", True)

    mf = deploy_module.load_manifest(
        fake_source_repo / "agents" / "testagent" / "manifest.json"
    )

    scripts_dir = fake_workspace / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    orphan = scripts_dir / "legacy.py"
    orphan.write_text("# dry-run preserves me\n", encoding="utf-8")

    deploy_module.sync_scripts(mf, yes_updates=True, remove_orphan_scripts=True)

    assert orphan.exists(), "dry-run must not delete"


def test_sync_scripts_orphan_flag_tolerates_missing_scripts_dir(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch
):
    """If the scripts/ dir doesn't exist yet (fresh workspace), the
    sweep is a no-op rather than a crash."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", fake_workspace.parent / "backups", raising=False)

    mf = deploy_module.load_manifest(
        fake_source_repo / "agents" / "testagent" / "manifest.json"
    )

    # Don't pre-create scripts/ — sync_scripts will create it under the
    # hood when it copies manifest-listed scripts, and the sweep should
    # happen after that without error.
    deploy_module.sync_scripts(mf, yes_updates=True, remove_orphan_scripts=True)
    # No assertion beyond "didn't raise"
