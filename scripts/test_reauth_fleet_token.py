"""Tests for scripts/reauth-fleet-token.py.

The helper bundles three steps the operator had to run by hand:

  1. Run the agent's interactive OAuth flow locally (browser-based).
  2. SCP the new token.json to the VPS.
  3. Verify the new token refreshes on the VPS.

Tests pin the alias resolution and the agent → auth-script mapping
(pure functions). Subprocess invocations are stubbed so the test
suite never runs a browser flow or shells out to ssh/scp.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "reauth-fleet-token.py"


def _import_module():
    spec = importlib.util.spec_from_file_location("reauth_fleet_token", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reauth_fleet_token"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Alias resolution ────────────────────────────────────────────────


@pytest.mark.parametrize("alias,canonical", [
    ("huckle", "connector"),
    ("Huckle", "connector"),
    ("HUCKLE-CAT", "connector"),
    ("connector", "connector"),
    ("mistress-mouse", "family-calendar"),
    ("mouse", "family-calendar"),
    ("family-calendar", "family-calendar"),
    ("murphy", "meetings-coach"),
    ("sergeant-murphy", "meetings-coach"),
    ("meetings-coach", "meetings-coach"),
    ("hilda", "shopping"),
    ("hippo", "shopping"),
    ("shopping", "shopping"),
])
def test_resolve_agent_aliases(alias, canonical):
    m = _import_module()
    assert m.resolve_agent(alias) == canonical


def test_resolve_agent_rejects_unknown():
    m = _import_module()
    with pytest.raises(ValueError):
        m.resolve_agent("unknown-agent")


# ─── Auth script + token path mapping ────────────────────────────────


def test_connector_auth_script_is_gmail_auth():
    m = _import_module()
    info = m.agent_paths("connector")
    assert info["auth_script"].name == "gmail-auth.py"
    assert "connector" in str(info["auth_script"])
    assert info["token_path"].name == "token.json"
    assert "connector-workspace" in str(info["token_path"])


def test_family_calendar_uses_gcal_auth():
    m = _import_module()
    info = m.agent_paths("family-calendar")
    assert info["auth_script"].name == "gcal-auth.py"
    assert "family-calendar" in str(info["auth_script"])


def test_meetings_coach_uses_gcal_auth():
    m = _import_module()
    info = m.agent_paths("meetings-coach")
    assert info["auth_script"].name == "gcal-auth.py"
    assert "meetings-coach" in str(info["auth_script"])


def test_paths_include_remote_destination():
    m = _import_module()
    info = m.agent_paths("connector")
    assert info["remote_path"] == ".clawford/connector-workspace/token.json"


# ─── End-to-end orchestration with stubbed subprocess ────────────────


def test_run_invokes_auth_then_scp_then_verify(monkeypatch, tmp_path):
    """Happy path: auth succeeds, token lands, scp succeeds, vps verify ok.

    All three subprocess calls happen in order, and the return code is 0.
    """
    m = _import_module()

    # Fake token landing on disk (the auth script "wrote" it)
    token_path = tmp_path / "fake-workspace" / "token.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(m, "agent_paths", lambda agent: {
        "auth_script": tmp_path / "fake-auth.py",
        "token_path": token_path,
        "remote_path": ".clawford/fake-workspace/token.json",
    })

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        out = MagicMock()
        out.returncode = 0
        out.stdout = "VPS refresh OK"
        return out

    monkeypatch.setattr(m.subprocess, "run", fake_run)

    rc = m.run_reauth("connector", remote_host="openclaw@<your-tailscale-host>")
    assert rc == 0
    # Three subprocess calls: auth, scp, ssh-verify
    assert len(calls) == 3
    assert "fake-auth.py" in " ".join(calls[0])
    assert calls[1][0] == "scp"
    assert calls[2][0] == "ssh"


def test_run_aborts_when_auth_fails(monkeypatch, tmp_path):
    """If the auth flow exits non-zero, abort before SCP."""
    m = _import_module()

    token_path = tmp_path / "ws" / "token.json"
    token_path.parent.mkdir(parents=True)

    monkeypatch.setattr(m, "agent_paths", lambda agent: {
        "auth_script": tmp_path / "auth.py",
        "token_path": token_path,
        "remote_path": "x",
    })

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        out = MagicMock()
        out.returncode = 1  # auth failed
        out.stdout = ""
        out.stderr = "user closed browser"
        return out

    monkeypatch.setattr(m.subprocess, "run", fake_run)

    rc = m.run_reauth("connector", remote_host="x@y")
    assert rc != 0
    # Only one call — auth — before bail
    assert len(calls) == 1


def test_run_aborts_when_token_not_written(monkeypatch, tmp_path):
    """If auth claims success but token.json wasn't written, abort.
    Catches the case where the operator declined the overwrite prompt."""
    m = _import_module()

    token_path = tmp_path / "ws" / "token.json"
    token_path.parent.mkdir(parents=True)
    # Don't create the token file

    monkeypatch.setattr(m, "agent_paths", lambda agent: {
        "auth_script": tmp_path / "auth.py",
        "token_path": token_path,
        "remote_path": "x",
    })

    def fake_run(cmd, **kwargs):
        out = MagicMock()
        out.returncode = 0
        return out

    monkeypatch.setattr(m.subprocess, "run", fake_run)

    rc = m.run_reauth("connector", remote_host="x@y")
    assert rc != 0


def test_run_aborts_when_vps_verify_fails(monkeypatch, tmp_path):
    """If VPS-side refresh fails, surface that — don't claim victory."""
    m = _import_module()

    token_path = tmp_path / "ws" / "token.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("{}")

    monkeypatch.setattr(m, "agent_paths", lambda agent: {
        "auth_script": tmp_path / "auth.py",
        "token_path": token_path,
        "remote_path": ".clawford/ws/token.json",
    })

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        out = MagicMock()
        # auth=ok, scp=ok, ssh-verify=fail
        out.returncode = 0 if call_count["n"] < 3 else 1
        out.stdout = "" if call_count["n"] < 3 else "REFRESH FAILED: invalid_grant"
        out.stderr = ""
        return out

    monkeypatch.setattr(m.subprocess, "run", fake_run)

    rc = m.run_reauth("connector", remote_host="x@y")
    assert rc != 0
