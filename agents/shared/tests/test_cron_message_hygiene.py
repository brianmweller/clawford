"""Tests for deploy.py Safeguard 9: cron message hygiene.

Safeguard 9 walks every cron in the manifest and refuses to deploy if
any cron `message` field contains a forbidden shell-operator pattern
(`; echo $?`, `sh -lc python`, `> /tmp/`, `2>&1`, `$(python`, etc.).
These are the bug-attractor patterns an LLM copies verbatim into its
exec command. OpenClaw 2026.4.11's hardcoded preflight check rejects
such commands before they run.

The broader hygiene contract is also enforced test-side in
`test_script_contract.py` (static parameterized check). Safeguard 9
is the deploy-time gate wired into `deploy_one()` — it blocks a
manifest edit that would reach the fleet with a bad message.

Exit code 8 on drift (following Safeguard 7 = exit 6 and Safeguard 8
= exit 7).
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pytest


def _make_args():
    return argparse.Namespace(
        agent_id="testagent",
        all=False, exclude=[], dry_run=False,
        skip_files=False, skip_scripts=False,
        skip_crons=True, skip_channel=True,
        remove_orphans=False, allow_dirty=False,
        yes_updates=True, accept_drift=True,
    )


def test_clean_manifest_passes(deploy_module, fake_source_repo):
    """An empty or clean crons list → empty error list."""
    manifest_path = fake_source_repo / "agents" / "testagent" / "manifest.json"
    mf = deploy_module.load_manifest(manifest_path)
    errors = deploy_module.check_cron_message_hygiene(mf)
    assert errors == []


def test_detects_semicolon_exit_capture(deploy_module, fake_source_repo):
    """`; echo $?` inside a cron message → flagged."""
    manifest_path = fake_source_repo / "agents" / "testagent" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["crons"] = [
        {
            "name": "bad-cron",
            "cron": "*/5 * * * *",
            "message": "Run python3 foo.py; echo $? to capture the exit code.",
            "announce": False,
            "no_deliver": True,
        }
    ]
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    mf = deploy_module.load_manifest(manifest_path)
    errors = deploy_module.check_cron_message_hygiene(mf)
    assert errors, "should flag the forbidden pattern"
    assert any("bad-cron" in e for e in errors), f"error should name the cron: {errors}"


def test_detects_sh_lc_wrap(deploy_module, fake_source_repo):
    """`sh -lc 'python3 ...'` wrapping → flagged."""
    manifest_path = fake_source_repo / "agents" / "testagent" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["crons"] = [
        {
            "name": "wrap-cron",
            "cron": "*/5 * * * *",
            "message": "Run: sh -lc 'python3 foo.py --flag'",
            "announce": False,
            "no_deliver": True,
        }
    ]
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    mf = deploy_module.load_manifest(manifest_path)
    errors = deploy_module.check_cron_message_hygiene(mf)
    assert errors
    assert any("wrap-cron" in e for e in errors)


def test_detects_stdout_redirect(deploy_module, fake_source_repo):
    """`> /tmp/...` output redirection → flagged."""
    manifest_path = fake_source_repo / "agents" / "testagent" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["crons"] = [
        {
            "name": "redir-cron",
            "cron": "*/5 * * * *",
            "message": "Run python3 foo.py > /tmp/out.json and parse it.",
            "announce": False,
            "no_deliver": True,
        }
    ]
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    mf = deploy_module.load_manifest(manifest_path)
    errors = deploy_module.check_cron_message_hygiene(mf)
    assert errors
    assert any("redir-cron" in e for e in errors)


def test_detects_update_your_status_file_clause(deploy_module, fake_source_repo):
    """Post-R6 regression guard: cron messages must not tell the LLM to
    write per-agent .status.md files. fleet-health.json is the authoritative
    health source — the LLM writing a status file drifts to whatever schema
    it picks (this is how the 2026-04-14 12:03 UTC brain-validation FAIL
    with '# family-calendar status' header happened)."""
    manifest_path = fake_source_repo / "agents" / "testagent" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["crons"] = [
        {
            "name": "legacy-cron",
            "cron": "30 10 * * *",
            "message": "Run python3 foo.py. Update your status file.",
            "announce": False,
            "no_deliver": True,
        }
    ]
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    mf = deploy_module.load_manifest(manifest_path)
    errors = deploy_module.check_cron_message_hygiene(mf)
    assert errors, "should flag the legacy status-file-write clause"
    assert any("legacy-cron" in e for e in errors), f"error should name the cron: {errors}"
    assert any("status file" in e.lower() or "Update your status" in e for e in errors), (
        f"error message should cite the violating clause: {errors}"
    )


def test_detects_multiple_violations_in_one_cron(deploy_module, fake_source_repo):
    """Multiple forbidden patterns in one message → all listed."""
    manifest_path = fake_source_repo / "agents" / "testagent" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["crons"] = [
        {
            "name": "triple-bad",
            "cron": "*/5 * * * *",
            "message": "sh -lc 'python3 foo.py' > /tmp/x 2>&1; the $? value matters",
            "announce": False,
            "no_deliver": True,
        }
    ]
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    mf = deploy_module.load_manifest(manifest_path)
    errors = deploy_module.check_cron_message_hygiene(mf)
    # At least one error per distinct forbidden pattern in the message.
    assert len(errors) >= 3
    assert all("triple-bad" in e for e in errors)


def test_deploy_one_refuses_when_cron_message_violates(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys
):
    """Top-level integration: a bad cron message → deploy_one refuses
    with non-zero exit before touching files."""
    monkeypatch.setattr(
        deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False
    )
    manifest_path = fake_source_repo / "agents" / "testagent" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["crons"] = [
        {
            "name": "oops",
            "cron": "*/5 * * * *",
            "message": "run: python3 foo.py; echo $? for exit code",
            "announce": False,
            "no_deliver": True,
        }
    ]
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Re-commit so source-clean check passes.
    import subprocess
    subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "add", "agents/testagent/manifest.json"],
        cwd=fake_source_repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-m", "inject bad cron"],
        cwd=fake_source_repo, check=True, capture_output=True,
    )

    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc != 0, "deploy must refuse when a cron message has forbidden patterns"
    out = capsys.readouterr().out
    assert "cron message" in out.lower() or "hygiene" in out.lower()
