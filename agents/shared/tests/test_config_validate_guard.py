"""Tests for the deploy.py openclaw-config validation safety gate.

P1's force-recreate downgraded the openclaw CLI and the older version
rejected the existing config's "streaming: {mode: ...}" object shape,
putting the gateway in a restart loop. This guard catches schema drift
at deploy time before any file writes happen.

The underlying implementation shells out to `openclaw config validate
--json` via the `oc_json` wrapper. The wrapper returns either:
  - {"valid": true, "path": "..."}                   — clean, return []
  - {"valid": false, "errors": [...], "path": "..."} — errors present
  - anything else / exception                        — treat as error
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_args(accept_drift=True):
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
        yes_updates=True,
        accept_drift=accept_drift,
    )


def test_check_openclaw_config_valid_returns_empty_on_valid(deploy_module, monkeypatch):
    """Happy path: openclaw config validate reports valid → empty list."""
    monkeypatch.setattr(
        deploy_module,
        "oc_json",
        lambda *a, **kw: {"valid": True, "path": "/home/node/.openclaw/openclaw.json"},
    )
    errors = deploy_module.check_openclaw_config_valid()
    assert errors == []


def test_check_openclaw_config_valid_returns_errors_on_invalid(deploy_module, monkeypatch):
    """Invalid config: errors list is extracted and stringified."""
    monkeypatch.setattr(
        deploy_module,
        "oc_json",
        lambda *a, **kw: {
            "valid": False,
            "errors": [
                "channels.telegram.streaming: Invalid input",
                "channels.telegram.accounts.newsdigest.streaming: Invalid input",
            ],
            "path": "/home/node/.openclaw/openclaw.json",
        },
    )
    errors = deploy_module.check_openclaw_config_valid()
    assert len(errors) == 2
    assert "channels.telegram.streaming" in errors[0]


def test_check_openclaw_config_valid_handles_dict_errors(deploy_module, monkeypatch):
    """Some validators return error objects instead of strings.
    The helper must flatten them to a readable string form."""
    monkeypatch.setattr(
        deploy_module,
        "oc_json",
        lambda *a, **kw: {
            "valid": False,
            "errors": [
                {"path": "channels.telegram.streaming", "message": "Invalid input"},
            ],
        },
    )
    errors = deploy_module.check_openclaw_config_valid()
    assert len(errors) == 1
    assert "channels.telegram.streaming" in errors[0]
    assert "Invalid input" in errors[0]


def test_check_openclaw_config_valid_handles_exception(deploy_module, monkeypatch):
    """If oc_json raises (gateway down, container crashed), return a
    single-element error list so deploy refuses to proceed."""
    def raiser(*a, **kw):
        raise RuntimeError("gateway container is restarting")
    monkeypatch.setattr(deploy_module, "oc_json", raiser)
    errors = deploy_module.check_openclaw_config_valid()
    assert len(errors) == 1
    assert "restarting" in errors[0] or "failed to run" in errors[0].lower()


def test_deploy_one_refuses_when_config_invalid(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys
):
    """Top-level integration: a broken openclaw.json → deploy_one returns
    a non-zero exit and prints the validation errors, BEFORE touching any
    files. No backup tarball, no cron edits."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    monkeypatch.setattr(
        deploy_module,
        "oc_json",
        lambda *a, **kw: {
            "valid": False,
            "errors": ["channels.telegram.streaming: Invalid input"],
        },
    )
    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc != 0, "deploy must refuse when config is invalid"
    out = capsys.readouterr().out
    assert "Config validation" in out or "config" in out.lower()
    # Backup dir must NOT exist — we refused before Safeguard 1 ran
    assert not (tmp_path / "backups").exists() or not any(
        (tmp_path / "backups").iterdir()
    )


def test_deploy_one_proceeds_when_config_valid(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path
):
    """Happy path: valid config → deploy proceeds normally."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    monkeypatch.setattr(
        deploy_module,
        "oc_json",
        lambda *a, **kw: {"valid": True, "path": "/fake"},
    )
    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc == 0
