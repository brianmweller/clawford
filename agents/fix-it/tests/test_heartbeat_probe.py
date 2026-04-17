"""Probe tests for fix-it/scripts/heartbeat.py.

Fix-it's probe reads ~/Dropbox/openclaw-backup/fleet-health.json
(written by fleet-health.py every */15 min) and reports on its
freshness. Cross-agent alerting is already handled by
fleet-health.py's summarize(); fix-it's probe only covers the one
thing summarize() can't — detecting that fleet-health.json itself
has stopped being written.

Contract:
  probe() returns a dict with keys:
    status        — "ok" | "error"
    checked       — number of agents present in fleet-health.json
    stale_count   — number of non-ok agents in fleet-health.json
    alert         — (optional) human-readable Telegram text
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_heartbeat():
    path = SCRIPTS_DIR / "heartbeat.py"
    spec = importlib.util.spec_from_file_location("fixit_heartbeat", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fixit_heartbeat"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def stub_fleet_health(tmp_path, monkeypatch):
    """Factory fixture: returns a function that writes a fake
    fleet-health.json with a controllable age + agent statuses."""
    brain = tmp_path / "openclaw-backup"
    brain.mkdir()
    fleet_health_path = brain / "fleet-health.json"

    mod = _load_heartbeat()
    monkeypatch.setattr(mod, "BRAIN", str(brain))
    monkeypatch.setattr(mod, "FLEET_HEALTH_PATH", str(fleet_health_path))

    def _write(agent_statuses: dict | None, age_minutes: int = 5) -> None:
        if agent_statuses is None:
            return  # deliberately leave fleet-health.json missing
        generated = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        report = {
            "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agents": {
                agent_id: {
                    "id": agent_id,
                    "probe_ts": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": status,
                    "probes": {},
                }
                for agent_id, status in agent_statuses.items()
            },
        }
        fleet_health_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return _write, mod


def test_probe_returns_ok_when_fleet_health_fresh_and_all_agents_ok(stub_fleet_health):
    write, mod = stub_fleet_health
    write({"connector": "ok", "shopping": "ok"}, age_minutes=5)
    result = mod.probe()
    assert result["status"] == "ok"
    assert result["checked"] == 2
    assert result["stale_count"] == 0
    assert "alert" not in result


def test_probe_errors_when_fleet_health_missing(stub_fleet_health):
    write, mod = stub_fleet_health
    write(None)  # don't write the file
    result = mod.probe()
    assert result["status"] == "error"
    assert "alert" in result
    assert "fleet-health" in result["alert"].lower()


def test_probe_errors_when_fleet_health_stale(stub_fleet_health):
    write, mod = stub_fleet_health
    write({"connector": "ok"}, age_minutes=120)
    result = mod.probe()
    assert result["status"] == "error"
    assert "alert" in result
    assert "fleet-health" in result["alert"].lower()


def test_probe_does_not_duplicate_cross_agent_alerts(stub_fleet_health):
    """When fleet-health.json is fresh but other agents are degraded,
    fix-it's probe should NOT alert (fleet-health.py's summarize()
    already surfaces those alerts). It should still count them in
    stale_count for visibility."""
    write, mod = stub_fleet_health
    write({"connector": "degraded", "shopping": "ok"}, age_minutes=5)
    result = mod.probe()
    assert result["status"] == "ok"
    assert result["checked"] == 2
    assert result["stale_count"] == 1
    assert "alert" not in result
