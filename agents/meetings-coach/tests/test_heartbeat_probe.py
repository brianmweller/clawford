"""R2 probe() contract tests for meetings-coach/scripts/heartbeat.py.

Companion to test_krisp_auth_status.py which exercises check_auth()
in isolation. This file verifies the probe() function added in R2:
it must be pure (no .status.md side effects) and return a dict
shaped for fleet-health.py serialization.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_heartbeat():
    path = SCRIPTS_DIR / "heartbeat.py"
    spec = importlib.util.spec_from_file_location("meetings_coach_heartbeat", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meetings_coach_heartbeat"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "meetings-coach-workspace"
    (ws / "cache" / "krisp-tokens").mkdir(parents=True)
    (ws / "cache" / "krisp-tokens" / "tokens.json").write_text(
        '{"access_token":"x","refresh_token":"y"}', encoding="utf-8"
    )
    (ws / "token.json").write_text('{"fake": true}', encoding="utf-8")
    # Required files per heartbeat.REQUIRED_FILES
    (ws / "meeting-config.json").write_text("{}")
    (ws / "sent-alerts.json").write_text("{}")
    monkeypatch.setenv("WORKFLOWY_API_KEY", "stub")

    mod = _load_heartbeat()
    monkeypatch.setattr(mod, "WORKSPACE", str(ws))
    return mod, ws


def test_probe_returns_dict_with_status_and_auth(fake_workspace):
    mod, ws = fake_workspace
    result = mod.probe()
    assert "status" in result
    assert result["status"] in ("ok", "degraded")
    assert "auth" in result
    assert "google_auth" in result["auth"]
    assert "workflowy_auth" in result["auth"]
    assert "krisp_auth" in result["auth"]


def test_probe_returns_degraded_when_required_file_missing(fake_workspace):
    mod, ws = fake_workspace
    (ws / "meeting-config.json").unlink()
    result = mod.probe()
    assert result["status"] == "degraded"
    assert "meeting-config.json" in result["missing_files"]
    assert "alert" in result


# ─── google_auth: must actually test credentials, not just file-exists ─


def test_google_auth_ok_when_credentials_refresh_successfully(fake_workspace, monkeypatch):
    """check_auth() must exercise get_credentials() and only report
    'ok' if the refresh round-trip succeeds. Regression guard for the
    2026-04-15 2.5-day silent outage where token.json existed and was
    valid JSON but the refresh_token had been revoked."""
    mod, ws = fake_workspace
    import types as _t
    monkeypatch.setattr(
        mod, "get_credentials",
        lambda creds_path, token_path, scopes:
        _t.SimpleNamespace(valid=True, refresh_token="ok"),
    )
    result = mod.check_auth()
    assert result["google_auth"] == "ok"


def test_google_auth_revoked_on_invalid_grant(fake_workspace, monkeypatch):
    mod, ws = fake_workspace
    def boom(creds_path, token_path, scopes):
        raise RuntimeError(
            "('invalid_grant: Token has been expired or revoked.', "
            "{'error': 'invalid_grant'})"
        )
    monkeypatch.setattr(mod, "get_credentials", boom)
    result = mod.check_auth()
    assert result["google_auth"] == "revoked"


def test_google_auth_error_on_other_exception(fake_workspace, monkeypatch):
    mod, ws = fake_workspace
    def boom(creds_path, token_path, scopes):
        raise OSError("network down")
    monkeypatch.setattr(mod, "get_credentials", boom)
    result = mod.check_auth()
    assert result["google_auth"] == "error"


def test_google_auth_missing_when_token_file_absent(fake_workspace):
    mod, ws = fake_workspace
    (ws / "token.json").unlink()
    result = mod.check_auth()
    assert result["google_auth"] == "missing"
