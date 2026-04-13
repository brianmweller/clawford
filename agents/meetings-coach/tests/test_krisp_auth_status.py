"""Test that meetings-coach heartbeat.py correctly classifies Krisp auth.

Before the P2 fix, heartbeat.py's Krisp check was a simple
"tokens.json exists and is non-empty" → "ok". That returned ok even
after Krisp server-side revoked the refresh_token, causing the
2026-04-12 audit to catch 46 consecutive post-meeting-scan 401s
while status.md reported krisp_auth: ok.

Now the check consults cache/krisp-last-401.json. When post-meeting-scan
observes a 401, it writes that file. heartbeat.py reads the file's
mtime: if within the last 60 min, krisp_auth = "expired". Older, or
no file, = "ok" (assume recovery).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
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
    """Set up a tmp workspace with a tokens.json shell and re-load heartbeat."""
    ws = tmp_path / "meetings-coach-workspace"
    (ws / "cache" / "krisp-tokens").mkdir(parents=True)
    token_file = ws / "cache" / "krisp-tokens" / "tokens.json"
    token_file.write_text('{"access_token":"x","refresh_token":"y"}', encoding="utf-8")
    # Google token also needs to load cleanly or google_auth=missing
    (ws / "token.json").write_text('{"fake": true}', encoding="utf-8")

    mod = _load_heartbeat()
    monkeypatch.setattr(mod, "WORKSPACE", str(ws))
    return {"mod": mod, "ws": ws}


def test_krisp_auth_ok_when_no_fail_marker(fake_workspace):
    """Happy path: tokens.json present, no krisp-last-401.json → ok."""
    auth = fake_workspace["mod"].check_auth()
    assert auth["krisp_auth"] == "ok"


def test_krisp_auth_missing_when_tokens_json_absent(fake_workspace, tmp_path):
    """No tokens.json at all → missing."""
    (fake_workspace["ws"] / "cache" / "krisp-tokens" / "tokens.json").unlink()
    auth = fake_workspace["mod"].check_auth()
    assert auth["krisp_auth"] == "missing"


def test_krisp_auth_expired_when_recent_401_marker(fake_workspace):
    """cache/krisp-last-401.json within the last 60 min → expired."""
    fail_file = fake_workspace["ws"] / "cache" / "krisp-last-401.json"
    fail_file.write_text('{"ts":"2026-04-13T00:00:00Z"}', encoding="utf-8")
    # Default mtime = now, so <60 min old → expired
    auth = fake_workspace["mod"].check_auth()
    assert auth["krisp_auth"] == "expired"


def test_krisp_auth_ok_when_old_401_marker(fake_workspace):
    """cache/krisp-last-401.json >60 min old → treated as recovered,
    status = ok. (A recent 401 is the only thing that should claim expired.)"""
    fail_file = fake_workspace["ws"] / "cache" / "krisp-last-401.json"
    fail_file.write_text('{}', encoding="utf-8")
    old_time = time.time() - (90 * 60)  # 90 min ago
    os.utime(str(fail_file), (old_time, old_time))

    auth = fake_workspace["mod"].check_auth()
    assert auth["krisp_auth"] == "ok"


def test_krisp_auth_fail_marker_takes_precedence_over_present_tokens(fake_workspace):
    """Even with a valid-looking tokens.json, a recent 401 marker
    means the tokens are dead — don't lie."""
    auth_before = fake_workspace["mod"].check_auth()
    assert auth_before["krisp_auth"] == "ok"

    # Write a fresh fail marker
    fail_file = fake_workspace["ws"] / "cache" / "krisp-last-401.json"
    fail_file.write_text('{}', encoding="utf-8")

    auth_after = fake_workspace["mod"].check_auth()
    assert auth_after["krisp_auth"] == "expired"
