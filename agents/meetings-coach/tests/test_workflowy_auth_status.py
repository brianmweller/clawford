"""Test that meetings-coach heartbeat.py correctly resolves workflowy_auth.

Before the 2026-04-15 fix, heartbeat.check_auth() checked only
os.environ.get("WORKFLOWY_API_KEY") — a naive one-liner that missed
the workspace `.env` fallback that `workflowy-sync.py` already used.

Under host-native Phase 6.5 cron the env var isn't exported, so the
heartbeat reported workflowy_auth=missing even though the key was
present in ~/.openclaw/meetings-coach-workspace/.env and workflowy-sync.py
was running fine against it. This test pins the fix: check_auth() must
mirror the same resolver as workflowy-sync.get_api_key().
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_heartbeat():
    path = SCRIPTS_DIR / "heartbeat.py"
    spec = importlib.util.spec_from_file_location("meetings_coach_heartbeat", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    """Workspace with a valid krisp tokens.json + google token.json, but
    WORKFLOWY_API_KEY deliberately NOT set in env — the point of these
    tests is to exercise the .env fallback chain."""
    ws = tmp_path / "meetings-coach-workspace"
    (ws / "cache" / "krisp-tokens").mkdir(parents=True)
    (ws / "cache" / "krisp-tokens" / "tokens.json").write_text(
        '{"access_token":"x","refresh_token":"y"}', encoding="utf-8"
    )
    (ws / "token.json").write_text('{"fake": true}', encoding="utf-8")

    monkeypatch.delenv("WORKFLOWY_API_KEY", raising=False)
    # Redirect HOME so any ~/openclaw/.env on the test runner's box
    # can't accidentally satisfy the resolver.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~", str(tmp_path)))

    mod = _load_heartbeat()
    monkeypatch.setattr(mod, "WORKSPACE", str(ws))
    return {"mod": mod, "ws": ws}


def test_workflowy_auth_ok_when_env_var_set(fake_workspace, monkeypatch):
    """Baseline: env var wins, even without any .env file."""
    monkeypatch.setenv("WORKFLOWY_API_KEY", "envstub")
    auth = fake_workspace["mod"].check_auth()
    assert auth["workflowy_auth"] == "ok"


def test_workflowy_auth_ok_from_workspace_env_file(fake_workspace):
    """The fix: no env var, but workspace/.env contains the key → ok.

    This is the Phase 6.5 host-cron case — deploy.py writes the key
    into ~/.openclaw/meetings-coach-workspace/.env, but the cron
    environment doesn't export it as a shell variable.
    """
    env_file = fake_workspace["ws"] / ".env"
    env_file.write_text("WORKFLOWY_API_KEY=filestub\n", encoding="utf-8")
    auth = fake_workspace["mod"].check_auth()
    assert auth["workflowy_auth"] == "ok"


def test_workflowy_auth_ok_from_workspace_env_quoted(fake_workspace):
    """Same fallback, but the value is quoted — mirror workflowy-sync.py's
    `.strip("'\\"")` behavior so both scripts agree on the key format."""
    env_file = fake_workspace["ws"] / ".env"
    env_file.write_text('WORKFLOWY_API_KEY="quoted-key"\n', encoding="utf-8")
    auth = fake_workspace["mod"].check_auth()
    assert auth["workflowy_auth"] == "ok"


def test_workflowy_auth_missing_when_neither_env_nor_file(fake_workspace):
    """No env var, no .env file anywhere → missing."""
    auth = fake_workspace["mod"].check_auth()
    assert auth["workflowy_auth"] == "missing"


def test_workflowy_auth_missing_when_env_file_has_empty_value(fake_workspace):
    """Edge case: .env file declares the var but with empty value → missing."""
    env_file = fake_workspace["ws"] / ".env"
    env_file.write_text("WORKFLOWY_API_KEY=\n", encoding="utf-8")
    auth = fake_workspace["mod"].check_auth()
    assert auth["workflowy_auth"] == "missing"
