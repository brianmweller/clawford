"""--skip-files should only skip config files, not scripts.

Regression: the post-merge git hook on the VPS (~/repo/.git/hooks/post-merge)
runs ``deploy.py --all --yes-updates --skip-files --skip-pip-audit`` after
every ``git pull``. Its purpose is to refresh the script copies in each
agent's workspace so cron jobs pick up new code. Pre-fix, ``--skip-files``
gated the WHOLE post-backup block (config files AND scripts AND shared
library AND state files AND symlink heal), so ``git pull`` silently left
every agent's cron path on stale code — exactly the failure mode the
hook was written to prevent (2026-04-25 incident, confirmed by running
``diff`` against the workspace directly after a hook-driven deploy).

New contract: ``--skip-files`` only skips Safeguard 10 (config source
resolution) and ``sync_files`` (SOUL.md / IDENTITY.md / USER.md copies).
Scripts, shared library, state-file preservation, and cross-workspace
symlink heal always run.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def _apply_args(agent_id: str = "testagent", **overrides):
    ns = argparse.Namespace(
        agent_id=agent_id,
        all=False,
        exclude=[],
        dry_run=False,
        skip_files=True,
        skip_scripts=False,
        skip_crons=True,
        skip_channel=True,
        remove_orphans=False,
        allow_dirty=False,
        yes_updates=True,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def test_skip_files_still_syncs_scripts(
    deploy_module, prepopulated_workspace, monkeypatch, tmp_path
):
    """With --skip-files, workspace scripts/ must still be refreshed from
    the source repo — this is what the post-merge git hook relies on to
    keep the cron path in sync after a plain ``git pull``."""
    monkeypatch.setattr(
        deploy_module, "BACKUPS_ROOT", tmp_path / "deploy-backups",
        raising=False,
    )
    # Sanity: prepopulated_workspace seeded v0-PRIOR; source repo has v1.
    prior = (prepopulated_workspace / "scripts" / "hello.py").read_text()
    assert "v0-PRIOR" in prior

    rc = deploy_module.deploy_one("testagent", _apply_args())
    assert rc == 0, f"deploy should succeed with --skip-files; got rc={rc}"

    after = (prepopulated_workspace / "scripts" / "hello.py").read_text()
    assert "hello v1" in after, (
        "--skip-files must not skip scripts/ sync — the post-merge hook "
        "relies on it. Workspace still has the pre-deploy content: "
        f"{after!r}"
    )


def test_skip_files_skips_config_files(
    deploy_module, prepopulated_workspace, monkeypatch, tmp_path
):
    """--skip-files must still skip config files. SOUL.md in the
    prepopulated workspace carries PRIOR content; post-Dropbox-brain
    migration the source repo has no SOUL.md at all, so sync_files
    would be a no-op regardless — but the gate itself must still
    short-circuit before Safeguard 10 would refuse a missing source."""
    monkeypatch.setattr(
        deploy_module, "BACKUPS_ROOT", tmp_path / "deploy-backups",
        raising=False,
    )
    # Guarantee the "prior" SOUL.md is still there afterwards — the
    # gate skipped it rather than overwriting.
    soul_before = (prepopulated_workspace / "SOUL.md").read_text()
    assert "PRIOR" in soul_before

    rc = deploy_module.deploy_one("testagent", _apply_args())
    assert rc == 0

    soul_after = (prepopulated_workspace / "SOUL.md").read_text()
    assert soul_after == soul_before, (
        "--skip-files must leave workspace config files untouched"
    )
