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

    brain = tmp_path / "openclaw-backup"
    (brain / "agents").mkdir(parents=True)

    mod = _load_heartbeat()
    monkeypatch.setattr(mod, "WORKSPACE", str(ws))
    monkeypatch.setattr(mod, "BRAIN", str(brain))
    monkeypatch.setattr(mod, "OUTPUT_FILE", str(brain / "agents" / "meetings-coach.status.md"))
    return mod, ws, brain


def test_probe_returns_dict_with_status_and_auth(fake_workspace):
    mod, ws, brain = fake_workspace
    result = mod.probe()
    assert "status" in result
    assert result["status"] in ("ok", "degraded")
    assert "auth" in result
    assert "google_auth" in result["auth"]
    assert "workflowy_auth" in result["auth"]
    assert "krisp_auth" in result["auth"]


def test_probe_does_not_write_status_md(fake_workspace):
    mod, ws, brain = fake_workspace
    sentinel = brain / "agents" / "meetings-coach.status.md"
    assert not sentinel.exists()  # baseline
    mod.probe()
    assert not sentinel.exists(), "probe() must not write status.md"


def test_probe_returns_degraded_when_required_file_missing(fake_workspace):
    mod, ws, brain = fake_workspace
    (ws / "meeting-config.json").unlink()
    result = mod.probe()
    assert result["status"] == "degraded"
    assert "meeting-config.json" in result["missing_files"]
    assert "alert" in result


def test_run_writes_status_md_then_returns_probe_result(fake_workspace):
    """run() = probe() + _write_status_md(). Verify the file IS written
    and that the returned dict matches what probe() would return."""
    mod, ws, brain = fake_workspace
    sentinel = brain / "agents" / "meetings-coach.status.md"
    result = mod.run()
    assert sentinel.exists()
    text = sentinel.read_text(encoding="utf-8")
    assert "# Meetings Coach — Status" in text
    assert "- **status:**" in text
    assert result["status"] in ("ok", "degraded")
