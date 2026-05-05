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


# ─── --all mode: one flow, fan out to every workspace ────────────────


def test_union_scopes_cover_every_per_agent_scope():
    """The union scope set must be a superset of every per-agent scope.

    If a future agent adds a scope that isn't in UNION_SCOPES, --all
    would silently narrow it. This test pins the contract.
    """
    m = _import_module()
    union = set(m.UNION_SCOPES)
    expected_minimums = {
        # Live token scopes observed on the VPS as of 2026-05-05.
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/pubsub",
        "https://www.googleapis.com/auth/tasks",
    }
    # calendar.readonly is implied by calendar (full); not a hard requirement.
    expected_minimums.discard("https://www.googleapis.com/auth/calendar.readonly")
    assert expected_minimums.issubset(union)


def test_run_all_writes_token_to_every_workspace(monkeypatch, tmp_path):
    """One OAuth flow → save_credentials called once per agent."""
    m = _import_module()

    # Redirect each workspace's token.json into tmp_path
    def fake_paths(agent):
        return {
            "auth_script": tmp_path / f"{agent}-auth.py",
            "token_path": tmp_path / f"{agent}-workspace" / "token.json",
            "remote_path": f".clawford/{agent}-workspace/token.json",
        }
    monkeypatch.setattr(m, "agent_paths", fake_paths)

    fake_creds = MagicMock(name="creds")
    monkeypatch.setattr(m, "_run_oauth_flow", lambda creds_path, scopes: fake_creds)

    written: list[Path] = []
    def fake_write(creds, token_path, scopes):
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}", encoding="utf-8")
        written.append(token_path)
    monkeypatch.setattr(m, "_write_token", fake_write)

    monkeypatch.setattr(m, "_canonical_credentials_path",
                        lambda: str(tmp_path / "creds.json"))
    (tmp_path / "creds.json").write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        out = MagicMock(); out.returncode = 0; out.stdout = "VPS refresh OK"; return out
    monkeypatch.setattr(m.subprocess, "run", fake_run)

    rc = m.run_reauth_all(remote_host="x@y")
    assert rc == 0
    # One token write per canonical agent
    agents = list(m._AGENT_CONFIG.keys())
    assert len(written) == len(agents)
    workspaces = {p.parent.name for p in written}
    assert workspaces == {f"{a}-workspace" for a in agents}


def test_run_all_runs_oauth_flow_exactly_once(monkeypatch, tmp_path):
    """The whole point of --all: one browser dance, not four."""
    m = _import_module()

    monkeypatch.setattr(m, "agent_paths", lambda agent: {
        "auth_script": tmp_path / f"{agent}-auth.py",
        "token_path": tmp_path / f"{agent}-ws" / "token.json",
        "remote_path": f".clawford/{agent}-ws/token.json",
    })

    flow_calls: list[tuple] = []
    def fake_flow(creds_path, scopes):
        flow_calls.append((creds_path, tuple(scopes)))
        return MagicMock(name="creds")
    monkeypatch.setattr(m, "_run_oauth_flow", fake_flow)

    monkeypatch.setattr(m, "_write_token", lambda c, p, s: (
        p.parent.mkdir(parents=True, exist_ok=True), p.write_text("{}", encoding="utf-8")
    ))
    monkeypatch.setattr(m, "_canonical_credentials_path",
                        lambda: str(tmp_path / "creds.json"))
    (tmp_path / "creds.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **kw: MagicMock(
        returncode=0, stdout="VPS refresh OK", stderr=""))

    rc = m.run_reauth_all(remote_host="x@y")
    assert rc == 0
    assert len(flow_calls) == 1, f"expected 1 flow, got {len(flow_calls)}"


def test_run_all_scps_and_verifies_each_agent(monkeypatch, tmp_path):
    """One scp + one ssh verify per agent."""
    m = _import_module()

    monkeypatch.setattr(m, "agent_paths", lambda agent: {
        "auth_script": tmp_path / f"{agent}-auth.py",
        "token_path": tmp_path / f"{agent}-ws" / "token.json",
        "remote_path": f".clawford/{agent}-ws/token.json",
    })
    monkeypatch.setattr(m, "_run_oauth_flow", lambda c, s: MagicMock())
    monkeypatch.setattr(m, "_write_token", lambda c, p, s: (
        p.parent.mkdir(parents=True, exist_ok=True), p.write_text("{}", encoding="utf-8")
    ))
    monkeypatch.setattr(m, "_canonical_credentials_path",
                        lambda: str(tmp_path / "creds.json"))
    (tmp_path / "creds.json").write_text("{}", encoding="utf-8")

    calls: list[list[str]] = []
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        out = MagicMock(); out.returncode = 0; out.stdout = "VPS refresh OK"; out.stderr = ""
        return out
    monkeypatch.setattr(m.subprocess, "run", fake_run)

    rc = m.run_reauth_all(remote_host="x@y")
    assert rc == 0

    n = len(m._AGENT_CONFIG)
    scps = [c for c in calls if c[0] == "scp"]
    sshs = [c for c in calls if c[0] == "ssh"]
    assert len(scps) == n, f"expected {n} scps, got {len(scps)}"
    assert len(sshs) == n, f"expected {n} ssh verifies, got {len(sshs)}"


def test_run_all_aborts_on_first_scp_failure(monkeypatch, tmp_path):
    """If one workspace fails to scp, halt — don't keep going."""
    m = _import_module()

    monkeypatch.setattr(m, "agent_paths", lambda agent: {
        "auth_script": tmp_path / f"{agent}-auth.py",
        "token_path": tmp_path / f"{agent}-ws" / "token.json",
        "remote_path": f".clawford/{agent}-ws/token.json",
    })
    monkeypatch.setattr(m, "_run_oauth_flow", lambda c, s: MagicMock())
    monkeypatch.setattr(m, "_write_token", lambda c, p, s: (
        p.parent.mkdir(parents=True, exist_ok=True), p.write_text("{}", encoding="utf-8")
    ))
    monkeypatch.setattr(m, "_canonical_credentials_path",
                        lambda: str(tmp_path / "creds.json"))
    (tmp_path / "creds.json").write_text("{}", encoding="utf-8")

    calls: list[list[str]] = []
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        out = MagicMock()
        # First scp fails; everything else would succeed
        out.returncode = 1 if cmd[0] == "scp" else 0
        out.stdout = "VPS refresh OK"
        out.stderr = ""
        return out
    monkeypatch.setattr(m.subprocess, "run", fake_run)

    rc = m.run_reauth_all(remote_host="x@y")
    assert rc != 0
    # Bailed after first failed scp — no ssh verify ever issued
    assert not any(c[0] == "ssh" for c in calls)


def test_main_all_flag_invokes_run_reauth_all(monkeypatch):
    """`--all` calls run_reauth_all and skips agent-positional resolution."""
    m = _import_module()

    called = {"all": False, "single": False}
    monkeypatch.setattr(m, "run_reauth_all",
                        lambda remote_host: called.__setitem__("all", True) or 0)
    monkeypatch.setattr(m, "run_reauth",
                        lambda *a, **kw: called.__setitem__("single", True) or 0)

    rc = m.main(["--all"])
    assert rc == 0
    assert called["all"] is True
    assert called["single"] is False


def test_main_requires_agent_or_all(monkeypatch, capsys):
    """No agent and no --all is a usage error."""
    m = _import_module()
    with pytest.raises(SystemExit):
        m.main([])
