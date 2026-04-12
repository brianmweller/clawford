"""Safeguard 1: pre-deploy workspace backup.

Before any file write, deploy.py must tar the target workspace to
~/.openclaw/deploy-backups/<agent>-<timestamp>.tar.gz. The backup
captures the PRE-WRITE state so rollback is trivial.
"""
from __future__ import annotations

import argparse
import tarfile
from pathlib import Path

import pytest


def _run_deploy_apply(deploy_module, agent_id: str = "testagent", **overrides):
    """Invoke deploy_one with apply-mode args (not --dry-run)."""
    ns = argparse.Namespace(
        agent_id=agent_id,
        all=False,
        exclude=[],
        dry_run=False,
        skip_files=False,
        skip_scripts=False,
        skip_crons=True,       # skip cron sync (stubbed anyway)
        skip_channel=True,     # skip channel setup (stubbed)
        remove_orphans=False,
        allow_dirty=False,
        yes_updates=True,      # backup tests isolate backup, skip the confirm gate
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return deploy_module.deploy_one(agent_id, ns)


def test_backup_tarball_exists_before_overwrite(
    deploy_module, prepopulated_workspace, monkeypatch, tmp_path
):
    """When deploy overwrites an existing workspace file, a backup tarball
    must exist at ~/.openclaw/deploy-backups/<agent>-<ts>.tar.gz containing
    the PRE-overwrite content of that file."""
    backups_dir = tmp_path / "deploy-backups"
    monkeypatch.setattr(
        deploy_module, "BACKUPS_ROOT", backups_dir, raising=False
    )
    rc = _run_deploy_apply(deploy_module)
    assert rc == 0, "deploy should succeed"

    tarballs = sorted(backups_dir.glob("testagent-*.tar.gz"))
    assert len(tarballs) >= 1, (
        f"expected at least one backup tarball in {backups_dir}, "
        f"found: {list(backups_dir.glob('*'))}"
    )

    latest = tarballs[-1]
    with tarfile.open(latest, "r:gz") as tf:
        names = tf.getnames()
        # The backup should contain the workspace files that EXISTED prior
        # to the overwrite. "SOUL.md" and "scripts/hello.py" are present in
        # the prepopulated workspace.
        assert any("SOUL.md" in n for n in names), f"SOUL.md missing from backup: {names}"
        assert any("scripts/hello.py" in n or "scripts\\hello.py" in n for n in names), (
            f"scripts/hello.py missing from backup: {names}"
        )
        # Extract SOUL.md and assert it's the PRE-write content
        for member in tf.getmembers():
            if member.name.endswith("SOUL.md"):
                f = tf.extractfile(member)
                assert f is not None
                content = f.read().decode("utf-8")
                assert "PRIOR" in content, (
                    f"backup captured post-write content, not pre-write: {content}"
                )
                break
        else:
            pytest.fail("SOUL.md member not extractable from backup")


def test_backup_directory_created_if_missing(
    deploy_module, prepopulated_workspace, monkeypatch, tmp_path
):
    """If ~/.openclaw/deploy-backups/ doesn't exist yet, deploy.py creates it."""
    backups_dir = tmp_path / "fresh-never-existed-backups"
    monkeypatch.setattr(
        deploy_module, "BACKUPS_ROOT", backups_dir, raising=False
    )
    assert not backups_dir.exists()
    _run_deploy_apply(deploy_module)
    assert backups_dir.exists(), "backup directory should be auto-created"


def test_backup_mirrors_to_dropbox(
    deploy_module, prepopulated_workspace, monkeypatch, tmp_path
):
    """Every backup tarball must also be written to the Dropbox mirror
    location so the user's local machine has an off-VPS copy with 180-day
    version history. This is the safety net that makes regression
    incidents recoverable from outside the VPS filesystem."""
    backups_dir = tmp_path / "deploy-backups"
    dropbox_dir = tmp_path / "dropbox-openclaw-backup" / "deploy-backups"
    monkeypatch.setattr(
        deploy_module, "BACKUPS_ROOT", backups_dir, raising=False
    )
    monkeypatch.setattr(
        deploy_module, "DROPBOX_BACKUP_ROOT", dropbox_dir, raising=False
    )
    _run_deploy_apply(deploy_module)

    # Primary backup exists
    primary = sorted(backups_dir.glob("testagent-*.tar.gz"))
    assert len(primary) == 1, f"primary backup missing: {list(backups_dir.iterdir())}"

    # Dropbox mirror exists with the same filename
    mirrored = sorted(dropbox_dir.glob("testagent-*.tar.gz"))
    assert len(mirrored) == 1, (
        f"dropbox mirror missing at {dropbox_dir}: "
        f"{list(dropbox_dir.iterdir()) if dropbox_dir.exists() else 'no dir'}"
    )
    assert mirrored[0].name == primary[0].name, "filenames must match"

    # Both tarballs have the same content (byte-equal)
    assert mirrored[0].read_bytes() == primary[0].read_bytes(), (
        "mirror content differs from primary — copy failed"
    )


def test_no_backup_when_workspace_is_empty(
    deploy_module, fake_workspace, monkeypatch, tmp_path
):
    """First-ever deploy into a fresh workspace has nothing to back up.
    Either skip the tarball entirely or write an empty one — test asserts
    the deploy succeeds without error, no crash on empty-workspace tar."""
    backups_dir = tmp_path / "deploy-backups"
    monkeypatch.setattr(
        deploy_module, "BACKUPS_ROOT", backups_dir, raising=False
    )
    rc = _run_deploy_apply(deploy_module)
    assert rc == 0, "first-deploy into empty workspace should succeed"
