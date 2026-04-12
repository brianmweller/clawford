"""Safeguard 6: post-deploy smoke test hook.

When --smoke-test is set, deploy.py fires a known-safe cron for the agent
(configured per-manifest) after apply and waits for the result. If the cron
fails, deploy.py restores the pre-deploy backup automatically and returns
a non-zero exit code. This is the circuit breaker for silent regressions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


def _make_args(smoke_test=True, yes_updates=True, accept_drift=True):
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
        allow_dirty=False,
        yes_updates=yes_updates,
        accept_drift=accept_drift,
        smoke_test=smoke_test,
    )


@pytest.fixture
def fake_source_repo_with_smoke(fake_source_repo: Path) -> Path:
    """Augment the base fake repo with a smoke_test entry in manifest.json."""
    manifest_path = fake_source_repo / "agents" / "testagent" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["smoke_test"] = {"cron_name": "heartbeat", "max_wait_s": 30}
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    import subprocess
    subprocess.run(["git", "add", "."], cwd=fake_source_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add smoke_test"],
        cwd=fake_source_repo, check=True, capture_output=True,
    )
    return fake_source_repo


def test_smoke_test_restores_backup_on_failure(
    deploy_module, fake_source_repo_with_smoke, prepopulated_workspace,
    monkeypatch, tmp_path,
):
    """When the smoke-test cron returns non-zero, deploy.py restores the
    pre-deploy backup and the workspace file content matches what was
    there before the apply.
    """
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)

    # Stub `run_smoke_test` to simulate a failing cron
    def fake_smoke(mf, cron_name, max_wait_s):
        return False  # False = failed
    monkeypatch.setattr(deploy_module, "run_smoke_test", fake_smoke, raising=False)

    original_soul = (prepopulated_workspace / "SOUL.md").read_text(encoding="utf-8")
    assert "PRIOR" in original_soul

    rc = deploy_module.deploy_one("testagent", _make_args(smoke_test=True))
    assert rc != 0, "failed smoke test should cause deploy to return non-zero"

    # Backup should have been restored — file content is back to pre-deploy state
    restored = (prepopulated_workspace / "SOUL.md").read_text(encoding="utf-8")
    assert "PRIOR" in restored, (
        f"backup restore did not happen, file is {restored!r}"
    )


def test_smoke_test_passes_when_cron_ok(
    deploy_module, fake_source_repo_with_smoke, prepopulated_workspace,
    monkeypatch, tmp_path,
):
    """When the smoke-test cron returns ok, deploy stays applied (no revert)."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)

    def fake_smoke(mf, cron_name, max_wait_s):
        return True  # True = passed
    monkeypatch.setattr(deploy_module, "run_smoke_test", fake_smoke, raising=False)

    rc = deploy_module.deploy_one("testagent", _make_args(smoke_test=True))
    assert rc == 0

    # File content should be the NEW (source) content, not the PRIOR
    post = (prepopulated_workspace / "SOUL.md").read_text(encoding="utf-8")
    assert "PRIOR" not in post, "deploy should have applied without reverting"


def test_smoke_test_skipped_when_flag_off(
    deploy_module, fake_source_repo_with_smoke, prepopulated_workspace,
    monkeypatch, tmp_path,
):
    """Without --smoke-test, run_smoke_test must not be called at all."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)

    def fake_smoke(*a, **k):
        raise AssertionError("run_smoke_test must not fire without --smoke-test")
    monkeypatch.setattr(deploy_module, "run_smoke_test", fake_smoke, raising=False)

    rc = deploy_module.deploy_one("testagent", _make_args(smoke_test=False))
    assert rc == 0
