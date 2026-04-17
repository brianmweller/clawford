"""Tests for family-calendar/scripts/heartbeat.py.

Deterministic probe: verify calendar-config.json and sent-reminders.json
exist, check google_auth (live refresh round-trip), prune stale reminders
(>48h), return SCRIPT_CONTRACT JSON.

`probe()` is pure; the fleet-health.py orchestrator invokes it via
probe-agent.py and writes the aggregated result to
<brain>/fleet-health.json. Per-agent status.md writes were retired.

Run: cd agents/family-calendar && python3 -m pytest tests/test_heartbeat.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"famcal_{name.replace('-', '_').replace('.py', '')}", path
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def stub_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "family-calendar-workspace"
    workspace.mkdir()

    # Required files
    (workspace / "calendar-config.json").write_text(json.dumps({
        "calendars": [
            {"id": "Sam@example.com", "label": "Sam", "emoji": "👨"},
            {"id": "Alex@example.com", "label": "Alex", "emoji": "👩"},
        ],
    }))
    (workspace / "sent-reminders.json").write_text(json.dumps({
        "reminders": {},
        "last_pruned": datetime.now(timezone.utc).isoformat(),
    }))

    # Google OAuth token — fresh mtime so google_auth probes "ok" by default
    (workspace / "token.json").write_text(json.dumps({
        "token": "fake", "refresh_token": "fake-refresh",
    }))

    hb = _load_script("heartbeat.py")
    monkeypatch.setattr(hb, "WORKSPACE", str(workspace))
    monkeypatch.setattr(hb, "CONFIG_FILE", str(workspace / "calendar-config.json"))
    monkeypatch.setattr(hb, "SENT_REMINDERS_FILE", str(workspace / "sent-reminders.json"))
    monkeypatch.setattr(hb, "TOKEN_FILE", str(workspace / "token.json"))

    return types.SimpleNamespace(
        hb=hb,
        workspace=workspace,
        config_file=workspace / "calendar-config.json",
        sent_file=workspace / "sent-reminders.json",
        token_file=workspace / "token.json",
    )


# ─── probe() — happy path + required files ──────────────────────────


def test_probe_returns_ok_when_all_present(stub_workspace):
    result = stub_workspace.hb.probe()
    assert result["status"] == "ok"
    assert result["google_auth"] == "ok"
    assert result["calendars_configured"] == 2
    assert "alert" not in result


def test_probe_returns_degraded_when_config_missing(stub_workspace):
    stub_workspace.config_file.unlink()
    result = stub_workspace.hb.probe()
    assert result["status"] == "degraded"
    assert "calendar-config.json" in result["missing_files"]


def test_probe_returns_degraded_when_sent_reminders_missing(stub_workspace):
    stub_workspace.sent_file.unlink()
    result = stub_workspace.hb.probe()
    assert result["status"] == "degraded"
    assert "sent-reminders.json" in result["missing_files"]


# ─── google_auth probe ──────────────────────────────────────────────


def test_probe_google_auth_missing_when_token_file_absent(stub_workspace):
    stub_workspace.token_file.unlink()
    result = stub_workspace.hb.probe()
    assert result["google_auth"] == "missing"
    assert result["status"] == "degraded"


def test_probe_google_auth_ok_when_credentials_refresh_successfully(stub_workspace, monkeypatch):
    """A live refresh round-trip succeeds → google_auth='ok'.

    Regression guard for the 2026-04-15 discovery: the old mtime-only
    check reported 'ok' even though the refresh_token had been revoked
    by Google 2.5 days earlier. The new check must actually exercise
    the credentials."""
    def fake_get_credentials(creds_path, token_path, scopes):
        return types.SimpleNamespace(valid=True, refresh_token="ok")

    monkeypatch.setattr(stub_workspace.hb, "get_credentials", fake_get_credentials)

    result = stub_workspace.hb.probe()
    assert result["google_auth"] == "ok"
    assert result["status"] == "ok"


def test_probe_google_auth_revoked_when_refresh_raises_invalid_grant(stub_workspace, monkeypatch):
    """This is the 2026-04-15 failure mode: refresh_token was revoked
    by Google (or expired after 6 months of inactivity), creds.refresh()
    raises RefreshError('invalid_grant'), and the fleet went blind for
    2.5 days because the probe only checked mtime. The new check must
    trip into 'revoked' and mark the agent degraded so fix-it fires
    an alert within one heartbeat cycle, not 2.5 days."""
    def failing_get_credentials(creds_path, token_path, scopes):
        raise RuntimeError(
            "('invalid_grant: Token has been expired or revoked.', "
            "{'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})"
        )

    monkeypatch.setattr(stub_workspace.hb, "get_credentials", failing_get_credentials)

    result = stub_workspace.hb.probe()
    assert result["google_auth"] == "revoked"
    assert result["status"] == "degraded"


def test_probe_google_auth_error_on_unexpected_exception(stub_workspace, monkeypatch):
    """Any other exception from get_credentials must surface as
    'error' (not silently pass as 'ok')."""
    def boom(creds_path, token_path, scopes):
        raise OSError("network down")

    monkeypatch.setattr(stub_workspace.hb, "get_credentials", boom)

    result = stub_workspace.hb.probe()
    assert result["google_auth"] == "error"
    assert result["status"] == "degraded"


# ─── sent-reminders prune ────────────────────────────────────────────


def test_probe_prunes_stale_reminders(stub_workspace):
    """Reminders older than 48h get removed from sent-reminders.json."""
    now = datetime.now(timezone.utc)
    sent_data = {
        "reminders": {
            "fresh_key_1": (now - timedelta(hours=12)).isoformat(),
            "stale_key_1": (now - timedelta(hours=72)).isoformat(),
            "stale_key_2": (now - timedelta(days=5)).isoformat(),
            "fresh_key_2": (now - timedelta(hours=24)).isoformat(),
        },
        "last_pruned": None,  # force prune
    }
    stub_workspace.sent_file.write_text(json.dumps(sent_data))

    result = stub_workspace.hb.probe()

    assert result["pruned_reminders"] == 2
    remaining = json.loads(stub_workspace.sent_file.read_text())
    assert "fresh_key_1" in remaining["reminders"]
    assert "fresh_key_2" in remaining["reminders"]
    assert "stale_key_1" not in remaining["reminders"]
    assert "stale_key_2" not in remaining["reminders"]


def test_probe_pruned_reminders_zero_when_all_fresh(stub_workspace):
    now = datetime.now(timezone.utc)
    sent_data = {
        "reminders": {
            f"key_{i}": (now - timedelta(hours=i)).isoformat()
            for i in range(5)
        },
        "last_pruned": None,
    }
    stub_workspace.sent_file.write_text(json.dumps(sent_data))
    result = stub_workspace.hb.probe()
    assert result["pruned_reminders"] == 0


# ─── main() wrapper ──────────────────────────────────────────────────


def test_main_prints_one_json_line_and_exits_zero(stub_workspace, capsys):
    rc = stub_workspace.hb.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    payload = json.loads(out)
    assert payload["status"] in ("ok", "degraded", "error")


def test_main_emits_error_json_when_probe_crashes(stub_workspace, capsys, monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(stub_workspace.hb, "probe", boom)
    rc = stub_workspace.hb.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert "alert" in payload


# ─── HeartbeatProbe subclass ────────────────────────────────


def test_family_calendar_probe_subclasses_heartbeat_probe(stub_workspace):
    """Carries AGENT_ID/TITLE/EMOJI for fleet-health aggregation and
    delegates probe() to the module-level function."""
    from agents.shared.heartbeat_base import HeartbeatProbe

    hb = stub_workspace.hb
    assert hasattr(hb, "FamilyCalendarProbe")
    cls = hb.FamilyCalendarProbe
    assert issubclass(cls, HeartbeatProbe)
    assert cls.AGENT_ID == "family-calendar"
    assert cls.TITLE == "Family Calendar"
    assert cls.EMOJI
