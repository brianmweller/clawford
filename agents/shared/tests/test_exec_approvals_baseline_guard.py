"""Tests for the deploy.py exec-approvals baseline drift guard.

After the 2026-04-13 "every agent shows approval required" incident,
we pinned an invariant baseline at ops/exec-approvals-baseline.json
(defaults.security=full, per-agent security/policy/ask all full/off)
and added a deploy.py Safeguard 8 that refuses to deploy if the live
exec-approvals state drifts from it.

The helper reads the baseline from disk and fetches live state via
oc_json("approvals", "get", "--json"). Flat-compares the specific
keys in the baseline — extra live allowlist entries are fine.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

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


# A "healthy" live approvals payload. Matches the baseline seeded by
# conftest's fake_source_repo (main + testagent). Extra allowlist entries
# are tolerated; the guard only checks keys present in the baseline.
HEALTHY_LIVE = {
    "defaults": {"security": "full", "ask": "off"},
    "agents": {
        "main":      {"security": "full", "policy": "full", "ask": "off", "allowlist": []},
        "testagent": {"security": "full", "policy": "full", "ask": "off", "allowlist": [{"pattern": "/usr/bin/*"}]},
    },
}


def test_baseline_passes_with_healthy_config(deploy_module, monkeypatch):
    """Healthy live state → empty error list."""
    def fake_oc_json(*args, **kw):
        if args[:2] == ("config", "validate"):
            return {"valid": True}
        if args[:2] == ("approvals", "get"):
            return HEALTHY_LIVE
        return {"jobs": []}
    monkeypatch.setattr(deploy_module, "oc_json", fake_oc_json)
    errors = deploy_module.check_exec_approvals_baseline()
    assert errors == []


def test_baseline_flags_allowlist_defaults(deploy_module, monkeypatch):
    """defaults.security='allowlist' → error listing the defaults mismatch."""
    live = json.loads(json.dumps(HEALTHY_LIVE))
    live["defaults"]["security"] = "allowlist"
    def fake_oc_json(*args, **kw):
        if args[:2] == ("config", "validate"): return {"valid": True}
        if args[:2] == ("approvals", "get"): return live
        return {"jobs": []}
    monkeypatch.setattr(deploy_module, "oc_json", fake_oc_json)
    errors = deploy_module.check_exec_approvals_baseline()
    assert any("defaults.security" in e and "allowlist" in e for e in errors)


def test_baseline_flags_missing_agent(deploy_module, monkeypatch):
    """Live config missing the 'testagent' agent → error naming it."""
    live = json.loads(json.dumps(HEALTHY_LIVE))
    del live["agents"]["testagent"]
    def fake_oc_json(*args, **kw):
        if args[:2] == ("config", "validate"): return {"valid": True}
        if args[:2] == ("approvals", "get"): return live
        return {"jobs": []}
    monkeypatch.setattr(deploy_module, "oc_json", fake_oc_json)
    errors = deploy_module.check_exec_approvals_baseline()
    assert any("testagent" in e for e in errors)


def test_baseline_flags_per_agent_policy_drift(deploy_module, monkeypatch):
    """main.policy='allowlist' → error pointing at main.policy."""
    live = json.loads(json.dumps(HEALTHY_LIVE))
    live["agents"]["main"]["policy"] = "allowlist"
    def fake_oc_json(*args, **kw):
        if args[:2] == ("config", "validate"): return {"valid": True}
        if args[:2] == ("approvals", "get"): return live
        return {"jobs": []}
    monkeypatch.setattr(deploy_module, "oc_json", fake_oc_json)
    errors = deploy_module.check_exec_approvals_baseline()
    assert any("main" in e and "policy" in e for e in errors)


def test_baseline_handles_wrapped_file_shape(deploy_module, monkeypatch):
    """openclaw 2026.4.11 `approvals get --json` wraps the actual data under
    a top-level `.file` key alongside `.path`, `.exists`, `.hash`, and
    `.effectivePolicy`. The helper must unwrap it so the guard compares
    against the real defaults/agents, not against an empty top level."""
    wrapped = {
        "path": "/home/node/.openclaw/exec-approvals.json",
        "exists": True,
        "hash": "deadbeef",
        "file": {
            "version": 1,
            "socket": {"path": "/fake.sock", "token": "xyz"},
            "defaults": {"security": "full", "ask": "off"},
            "agents": {
                "main":      {"security": "full", "policy": "full", "ask": "off", "allowlist": []},
                "testagent": {"security": "full", "policy": "full", "ask": "off", "allowlist": []},
            },
        },
        "effectivePolicy": {"scopes": []},
    }
    def fake_oc_json(*args, **kw):
        if args[:2] == ("config", "validate"): return {"valid": True}
        if args[:2] == ("approvals", "get"): return wrapped
        return {"jobs": []}
    monkeypatch.setattr(deploy_module, "oc_json", fake_oc_json)
    errors = deploy_module.check_exec_approvals_baseline()
    assert errors == [], f"wrapped .file shape must be unwrapped; got {errors}"


def test_baseline_tolerates_extra_allowlist_entries(deploy_module, monkeypatch):
    """Live allowlist patterns are volatile; baseline ignores them."""
    live = json.loads(json.dumps(HEALTHY_LIVE))
    live["agents"]["testagent"]["allowlist"] = [
        {"pattern": "/usr/bin/find", "lastUsedAt": 1776051588282},
        {"pattern": "/bin/ls"},
        {"pattern": "python3 *"},
    ]
    def fake_oc_json(*args, **kw):
        if args[:2] == ("config", "validate"): return {"valid": True}
        if args[:2] == ("approvals", "get"): return live
        return {"jobs": []}
    monkeypatch.setattr(deploy_module, "oc_json", fake_oc_json)
    errors = deploy_module.check_exec_approvals_baseline()
    assert errors == []


def test_deploy_one_refuses_when_exec_approvals_drift(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys
):
    """Top-level: drift → non-zero exit with errors logged, no file writes."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    bad_live = json.loads(json.dumps(HEALTHY_LIVE))
    bad_live["defaults"]["security"] = "allowlist"
    def fake_oc_json(*args, **kw):
        if args[:2] == ("config", "validate"): return {"valid": True}
        if args[:2] == ("approvals", "get"): return bad_live
        return {"jobs": []}
    monkeypatch.setattr(deploy_module, "oc_json", fake_oc_json)
    rc = deploy_module.deploy_one("testagent", _make_args())
    assert rc != 0
    out = capsys.readouterr().out
    assert "exec-approvals" in out.lower() or "defaults.security" in out
