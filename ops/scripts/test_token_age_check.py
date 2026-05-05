"""Tests for ops/scripts/token-age-check.py.

Daily cron that proactively warns the operator before any agent's OAuth
refresh-token hits Google's 7-day expiry cliff (the rule that bit
Huckle Cat — applies to unverified production apps using
restricted/sensitive scopes like gmail.readonly / gmail.compose,
which is the whole fleet's situation).

The check categorizes each agent's token by file mtime:

  fresh    < 5 days old      → silent
  warn     5..7 days old     → page with "re-auth in next 2 days"
  expired  >= 7 days old     → page with "re-auth NOW, already broken"

Tests pin the categorization logic (pure function, no I/O), the
envelope shape (matches SCRIPT_CONTRACT v2 with `alert` field), and
the envelope-resolution end-to-end against tmp_path with a fabricated
manifest.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "scripts" / "token-age-check.py"


def _import_module():
    """Import token-age-check.py as a module (the hyphen in its name
    blocks `import` syntax; we do it through importlib)."""
    spec = importlib.util.spec_from_file_location("token_age_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["token_age_check"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Pure categorization ─────────────────────────────────────────────


def test_categorize_fresh_for_recent_token():
    m = _import_module()
    assert m.categorize(age_days=0.5) == "fresh"
    assert m.categorize(age_days=4.99) == "fresh"


def test_categorize_warn_for_5_to_7_days():
    m = _import_module()
    assert m.categorize(age_days=5.0) == "warn"
    assert m.categorize(age_days=6.5) == "warn"
    assert m.categorize(age_days=6.99) == "warn"


def test_categorize_expired_at_seven_days():
    m = _import_module()
    assert m.categorize(age_days=7.0) == "expired"
    assert m.categorize(age_days=10.0) == "expired"


def test_categorize_missing_when_age_is_none():
    """A missing token file gets its own category — "no token,
    nothing to re-auth" is different from "stale token, re-auth"."""
    m = _import_module()
    assert m.categorize(age_days=None) == "missing"


# ─── scan_agents — walks fleet manifest ──────────────────────────────


def _make_manifest(tmp_path: Path, agents: list[dict]) -> Path:
    """Write a minimal fleet-manifest.json shaped like the real one."""
    manifest = {"version": 1, "agents": agents}
    p = tmp_path / "fleet-manifest.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    return p


def _make_token(workspace: Path, *, age_days: float | None) -> Path:
    """Create token.json under workspace. age_days=None → file absent."""
    workspace.mkdir(parents=True, exist_ok=True)
    token = workspace / "token.json"
    if age_days is None:
        return token  # don't create
    token.write_text("{}", encoding="utf-8")
    mtime = time.time() - (age_days * 86400)
    os.utime(token, (mtime, mtime))
    return token


def test_scan_agents_categorizes_each_token(tmp_path: Path):
    m = _import_module()

    fresh_ws = tmp_path / "fresh-workspace"
    warn_ws = tmp_path / "warn-workspace"
    expired_ws = tmp_path / "expired-workspace"
    missing_ws = tmp_path / "missing-workspace"
    missing_ws.mkdir()  # workspace exists, but no token.json

    _make_token(fresh_ws, age_days=1.0)
    _make_token(warn_ws, age_days=6.0)
    _make_token(expired_ws, age_days=8.0)

    manifest = _make_manifest(tmp_path, [
        {"id": "fresh", "display_name": "Fresh", "workspace": str(fresh_ws)},
        {"id": "warn", "display_name": "Warn", "workspace": str(warn_ws)},
        {"id": "expired", "display_name": "Expired", "workspace": str(expired_ws)},
        {"id": "missing", "display_name": "Missing", "workspace": str(missing_ws)},
    ])

    rows = m.scan_agents(str(manifest))
    by_id = {r["id"]: r for r in rows}

    assert by_id["fresh"]["category"] == "fresh"
    assert by_id["warn"]["category"] == "warn"
    assert by_id["expired"]["category"] == "expired"
    assert by_id["missing"]["category"] == "missing"

    # Ages roughly match what we set
    assert 0.9 < by_id["fresh"]["age_days"] < 1.1
    assert 5.9 < by_id["warn"]["age_days"] < 6.1
    assert 7.9 < by_id["expired"]["age_days"] < 8.1
    assert by_id["missing"]["age_days"] is None


def test_scan_agents_expands_user_in_workspace_path(tmp_path: Path, monkeypatch):
    """Manifest entries use ~/.clawford/... paths; scan must expand them."""
    m = _import_module()

    # Pretend HOME is tmp_path so ~/.clawford resolves there.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows fallback

    workspace = tmp_path / ".clawford" / "demo-workspace"
    _make_token(workspace, age_days=2.0)

    manifest = _make_manifest(tmp_path, [
        {
            "id": "demo",
            "display_name": "Demo",
            "workspace": "~/.clawford/demo-workspace",
        },
    ])

    rows = m.scan_agents(str(manifest))
    assert rows[0]["category"] == "fresh"


# ─── build_envelope — formats the SCRIPT_CONTRACT result ─────────────


def test_envelope_status_ok_when_all_fresh():
    m = _import_module()
    rows = [
        {"id": "a", "display_name": "A", "category": "fresh", "age_days": 1.0},
        {"id": "b", "display_name": "B", "category": "fresh", "age_days": 3.0},
    ]
    env = m.build_envelope(rows)
    assert env["status"] == "ok"
    assert "alert" not in env or env.get("alert") in (None, "")


def test_envelope_status_error_when_any_warn(tmp_path: Path):
    m = _import_module()
    rows = [
        {"id": "fresh", "display_name": "Fresh", "category": "fresh", "age_days": 1.0},
        {"id": "huckle", "display_name": "Huckle Cat", "category": "warn", "age_days": 5.5},
    ]
    env = m.build_envelope(rows)
    assert env["status"] == "error"
    assert env["alert"]
    assert "Huckle" in env["alert"] or "huckle" in env["alert"]
    assert "5" in env["alert"] or "5.5" in env["alert"]


def test_envelope_status_error_lists_expired_first(tmp_path: Path):
    """Expired agents need re-auth NOW; warn agents have headroom.
    Order: expired first, then warn."""
    m = _import_module()
    rows = [
        {"id": "warn-a", "display_name": "Warn A", "category": "warn", "age_days": 5.0},
        {"id": "expired-b", "display_name": "Expired B", "category": "expired", "age_days": 8.0},
    ]
    env = m.build_envelope(rows)
    alert = env["alert"]
    assert env["status"] == "error"
    # Expired should appear before warn in the alert text
    assert alert.index("Expired B") < alert.index("Warn A")


def test_envelope_alert_includes_reauth_helper_path():
    """The alert should give the operator a concrete next step — the
    re-auth helper command — so he doesn't have to look it up."""
    m = _import_module()
    rows = [{"id": "x", "display_name": "X", "category": "expired", "age_days": 9.0}]
    env = m.build_envelope(rows)
    assert "reauth" in env["alert"].lower() or "re-auth" in env["alert"].lower()


def test_envelope_uses_signed_link_when_daemon_responds(monkeypatch):
    """If the VPS-side daemon mints a tailnet link, the alert embeds it
    so the operator can tap from his phone without leaving the page."""
    m = _import_module()
    fake_link = "https://<your-tailscale-host>.tail106e99.ts.net/oauth/start?t=fake.sig"
    monkeypatch.setattr(m, "_mint_signed_link", lambda: fake_link)
    rows = [{"id": "x", "display_name": "X", "category": "expired", "age_days": 9.0}]
    env = m.build_envelope(rows)
    assert fake_link in env["alert"]


def test_envelope_falls_back_to_laptop_command_when_no_link(monkeypatch):
    """If link generation fails (daemon down, no creds, etc.), the
    alert still fires with the legacy laptop-side command."""
    m = _import_module()
    monkeypatch.setattr(m, "_mint_signed_link", lambda: None)
    rows = [{"id": "x", "display_name": "X", "category": "expired", "age_days": 9.0}]
    env = m.build_envelope(rows)
    assert env["status"] == "error"
    assert "reauth-fleet-token" in env["alert"] or "--all" in env["alert"]


def test_envelope_skips_missing_tokens_silently():
    """An agent with no token.json hasn't been authed yet — that's
    not a re-auth situation, so don't page about it. (The first auth
    is a separate user-driven workflow.)"""
    m = _import_module()
    rows = [
        {"id": "fresh", "display_name": "Fresh", "category": "fresh", "age_days": 1.0},
        {"id": "missing", "display_name": "Missing", "category": "missing", "age_days": None},
    ]
    env = m.build_envelope(rows)
    assert env["status"] == "ok"


# ─── End-to-end: invoke as subprocess, parse envelope ────────────────


def test_main_emits_compliant_envelope_when_all_fresh(tmp_path: Path):
    workspace = tmp_path / "fresh-workspace"
    _make_token(workspace, age_days=1.0)
    manifest = _make_manifest(tmp_path, [
        {"id": "fresh", "display_name": "Fresh", "workspace": str(workspace)},
    ])

    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(manifest)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    last = res.stdout.strip().splitlines()[-1]
    env = json.loads(last)
    assert env["status"] == "ok"


def test_main_emits_alert_envelope_for_expired_token(tmp_path: Path):
    workspace = tmp_path / "expired-workspace"
    _make_token(workspace, age_days=8.0)
    manifest = _make_manifest(tmp_path, [
        {"id": "huckle", "display_name": "Huckle Cat", "workspace": str(workspace)},
    ])

    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(manifest)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    last = res.stdout.strip().splitlines()[-1]
    env = json.loads(last)
    assert env["status"] == "error"
    assert env["alert"]
    assert "Huckle" in env["alert"]
