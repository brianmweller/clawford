"""Probe tests for fix-it/scripts/heartbeat.py.

R4+ semantics: fix-it's probe reads ~/Dropbox/openclaw-backup/
fleet-health.json (written by fleet-health.py every */15 min) and
reports on its freshness — NOT on per-agent .status.md files.

Rationale: the R3 orchestrator retired the per-agent heartbeat
crons that used to write .status.md, so scraping them produces
false positives. Cross-agent alerting is already handled by
fleet-health.py's summarize() — fix-it's probe only covers the
one thing summarize() can't: detecting that fleet-health.json
itself has stopped being written.

Contract (must survive this refactor):
  probe() returns a dict with keys:
    status        — "ok" | "error"
    checked       — number of agents present in fleet-health.json
    stale_count   — number of non-ok agents in fleet-health.json
    alert         — (optional) human-readable Telegram text
  probe() does NOT write fix-it.status.md (side-effect free).
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
    fleet-health.json with a controllable age + agent statuses, and
    returns (module, brain_dir) with BRAIN/FLEET_HEALTH_PATH/
    STATUS_DIR/OUTPUT_FILE monkeypatched onto the module."""
    brain = tmp_path / "openclaw-backup"
    status_dir = brain / "agents"
    status_dir.mkdir(parents=True)
    fleet_health_path = brain / "fleet-health.json"

    mod = _load_heartbeat()
    monkeypatch.setattr(mod, "BRAIN", str(brain))
    monkeypatch.setattr(mod, "STATUS_DIR", str(status_dir))
    monkeypatch.setattr(mod, "OUTPUT_FILE", str(status_dir / "fix-it.status.md"))
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

    return _write, mod, brain, status_dir


def test_probe_returns_ok_when_fleet_health_fresh_and_all_agents_ok(stub_fleet_health):
    write, mod, brain, status_dir = stub_fleet_health
    write({"connector": "ok", "shopping": "ok"}, age_minutes=5)
    result = mod.probe()
    assert result["status"] == "ok"
    assert result["checked"] == 2
    assert result["stale_count"] == 0
    assert "alert" not in result


def test_probe_does_not_write_status_md(stub_fleet_health):
    write, mod, brain, status_dir = stub_fleet_health
    write({"connector": "ok"}, age_minutes=5)
    sentinel = status_dir / "fix-it.status.md"
    assert not sentinel.exists()
    mod.probe()
    assert not sentinel.exists(), "probe() must not write status.md"


def test_probe_errors_when_fleet_health_missing(stub_fleet_health):
    write, mod, brain, status_dir = stub_fleet_health
    write(None)  # don't write the file
    result = mod.probe()
    assert result["status"] == "error"
    assert "alert" in result
    assert "fleet-health" in result["alert"].lower()


def test_probe_errors_when_fleet_health_stale(stub_fleet_health):
    write, mod, brain, status_dir = stub_fleet_health
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
    write, mod, brain, status_dir = stub_fleet_health
    write({"connector": "degraded", "shopping": "ok"}, age_minutes=5)
    result = mod.probe()
    assert result["status"] == "ok"
    assert result["checked"] == 2
    assert result["stale_count"] == 1
    assert "alert" not in result


def test_run_writes_status_md_then_returns_probe_result(stub_fleet_health):
    write, mod, brain, status_dir = stub_fleet_health
    write({"connector": "ok", "shopping": "ok"}, age_minutes=5)
    sentinel = status_dir / "fix-it.status.md"
    result = mod.run()
    assert sentinel.exists()
    text = sentinel.read_text(encoding="utf-8")
    assert "# Fix-It — Status" in text
    assert "- **status:** healthy" in text
    assert result["status"] == "ok"
