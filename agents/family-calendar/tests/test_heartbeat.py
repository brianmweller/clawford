"""Tests for family-calendar/scripts/heartbeat.py.

Replaces the LLM-native heartbeat cron with a deterministic Python
probe: verify calendar-config.json and sent-reminders.json exist,
check google_auth (token.json mtime), prune stale reminders (>48h),
write family-calendar.status.md, return SCRIPT_CONTRACT JSON.

Two-layer design: probe() is pure, run() writes status.md, main()
wraps run() in try/except per SCRIPT_CONTRACT. See
agents/connector/tests/test_heartbeat.py for the template.

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
    brain = tmp_path / "openclaw-backup"
    (brain / "agents").mkdir(parents=True)

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
    monkeypatch.setattr(hb, "BRAIN", str(brain))
    monkeypatch.setattr(hb, "OUTPUT_FILE", str(brain / "agents" / "family-calendar.status.md"))
    monkeypatch.setattr(hb, "CONFIG_FILE", str(workspace / "calendar-config.json"))
    monkeypatch.setattr(hb, "SENT_REMINDERS_FILE", str(workspace / "sent-reminders.json"))
    monkeypatch.setattr(hb, "TOKEN_FILE", str(workspace / "token.json"))

    return types.SimpleNamespace(
        hb=hb,
        workspace=workspace,
        brain=brain,
        config_file=workspace / "calendar-config.json",
        sent_file=workspace / "sent-reminders.json",
        token_file=workspace / "token.json",
        status_file=brain / "agents" / "family-calendar.status.md",
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


def test_probe_google_auth_stale_when_token_missing(stub_workspace):
    stub_workspace.token_file.unlink()
    result = stub_workspace.hb.probe()
    assert result["google_auth"] == "missing"
    assert result["status"] == "degraded"


def test_probe_google_auth_stale_when_token_mtime_old(stub_workspace):
    """Token mtime >30 days → google_auth=stale. The refresh flow
    should touch token.json on every gcal-fetch.py call; an ancient
    mtime means the flow is broken."""
    old_ts = time.time() - (40 * 86400)
    os.utime(str(stub_workspace.token_file), (old_ts, old_ts))
    result = stub_workspace.hb.probe()
    assert result["google_auth"] == "stale"
    assert result["status"] == "degraded"


def test_probe_google_auth_ok_when_token_fresh(stub_workspace):
    """Token updated within the last 30 days → ok."""
    # Default fixture already writes a fresh token
    result = stub_workspace.hb.probe()
    assert result["google_auth"] == "ok"


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


# ─── status.md write ─────────────────────────────────────────────────


def test_run_writes_status_md_with_core_fields(stub_workspace):
    stub_workspace.hb.run()
    text = stub_workspace.status_file.read_text(encoding="utf-8")
    assert "# Family Calendar — Status" in text
    assert "- **last_heartbeat:**" in text
    assert "- **status:** ok" in text
    assert "- **google_auth:** ok" in text
    assert "- **calendars_configured:** 2" in text
    assert "- **error_log:**" in text


def test_run_marks_google_auth_in_status_md_when_missing(stub_workspace):
    stub_workspace.token_file.unlink()
    stub_workspace.hb.run()
    text = stub_workspace.status_file.read_text(encoding="utf-8")
    assert "- **google_auth:** missing" in text
    assert "- **status:** degraded" in text


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


# ─── Phase 4: HeartbeatProbe subclass ────────────────────────────────


def test_family_calendar_probe_subclasses_heartbeat_probe(stub_workspace):
    """Phase 4: family-calendar/heartbeat.py exposes a
    FamilyCalendarProbe class that subclasses
    agents.shared.heartbeat_base.HeartbeatProbe, matching the fleet
    convention. The class carries AGENT_ID, TITLE, EMOJI identifiers
    and delegates probe()/render_status_md() to per-agent logic."""
    from agents.shared.heartbeat_base import HeartbeatProbe

    hb = stub_workspace.hb
    assert hasattr(hb, "FamilyCalendarProbe"), (
        "expected FamilyCalendarProbe class to exist after Phase 4 refactor"
    )
    cls = hb.FamilyCalendarProbe
    assert issubclass(cls, HeartbeatProbe)
    assert cls.AGENT_ID == "family-calendar"
    assert cls.TITLE == "Family Calendar"
    assert cls.EMOJI  # any non-empty emoji is acceptable


def test_family_calendar_probe_render_status_md_shape(stub_workspace):
    """render_status_md() returns the markdown body that was previously
    inlined in _write_status_md. Must include the same fields."""
    probe_result = {
        "status": "ok",
        "google_auth": "ok",
        "calendars_configured": 2,
        "pruned_reminders": 1,
        "missing_files": [],
        "last_cron_run": "2026-04-14T10:00:00Z",
        "last_cron_name": "morning-briefing",
        "last_cron_result": "14 events",
    }
    hb = stub_workspace.hb
    instance = hb.FamilyCalendarProbe()
    md = instance.render_status_md(probe_result)

    assert "# Family Calendar — Status" in md
    assert "**status:** ok" in md
    assert "**google_auth:** ok" in md
    assert "**calendars_configured:** 2" in md
    assert "**pruned_reminders:** 1" in md
    assert "**last_cron_run:** 2026-04-14T10:00:00Z — morning-briefing" in md
    assert "**last_cron_result:** 14 events" in md
    assert "**error_log:** none" in md
