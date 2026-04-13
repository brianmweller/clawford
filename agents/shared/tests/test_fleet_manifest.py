"""Tests for agents/shared/fleet_health_types.py + fleet-manifest.json.

R1 of the registry-based agent health system. Introduces the schema
for a fleet manifest that the R3 orchestrator consumes to know which
agents exist, how to invoke their probes, and where to read/write
their results.

This file covers:
  - FleetManifest / AgentHealthSpec dataclasses (schema)
  - load_fleet_manifest() loader + validation
  - ProbeResult / AgentProbeReport / FleetHealthReport dataclasses
    (the shape fleet-health.py will produce)
  - Round-trip serialization through dict for all types

Run: cd agents/shared && python3 -m pytest tests/test_fleet_manifest.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent


def _load_types():
    path = SHARED_DIR / "fleet_health_types.py"
    spec = importlib.util.spec_from_file_location("fleet_health_types", path)
    m = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so dataclasses'
    # ForwardRef resolution (which does sys.modules[cls.__module__])
    # can find the module during class definition.
    sys.modules["fleet_health_types"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def types_module():
    return _load_types()


@pytest.fixture
def valid_manifest_dict():
    """A minimal but fully-populated fleet manifest dict."""
    return {
        "version": 1,
        "agents": [
            {
                "id": "shopping",
                "display_name": "Hilda Hippo",
                "workspace": "~/.openclaw/shopping-workspace",
                "bot_token_env": "SHOPPING_BOT_TOKEN",
                "probe_entrypoint": "scripts/heartbeat.py::probe",
                "expected_probes": ["costco_session", "amazon_session"],
            },
            {
                "id": "connector",
                "display_name": "Huckle Cat",
                "workspace": "~/.openclaw/connector-workspace",
                "bot_token_env": "CONNECTOR_BOT_TOKEN",
                "probe_entrypoint": "scripts/heartbeat.py::probe",
                "expected_probes": ["missing_files"],
            },
        ],
    }


# ─── AgentHealthSpec ────────────────────────────────────────────────


def test_agent_health_spec_from_dict(types_module, valid_manifest_dict):
    spec = types_module.AgentHealthSpec.from_dict(valid_manifest_dict["agents"][0])
    assert spec.id == "shopping"
    assert spec.display_name == "Hilda Hippo"
    assert spec.bot_token_env == "SHOPPING_BOT_TOKEN"
    assert spec.expected_probes == ["costco_session", "amazon_session"]


def test_agent_health_spec_expands_workspace_home(types_module):
    spec = types_module.AgentHealthSpec.from_dict({
        "id": "test",
        "display_name": "Test",
        "workspace": "~/.openclaw/test-workspace",
        "bot_token_env": "TEST_BOT_TOKEN",
        "probe_entrypoint": "scripts/heartbeat.py::probe",
        "expected_probes": [],
    })
    # Expansion happens on read
    assert spec.workspace_expanded() == os.path.expanduser("~/.openclaw/test-workspace")


def test_agent_health_spec_parses_entrypoint(types_module):
    spec = types_module.AgentHealthSpec.from_dict({
        "id": "test",
        "display_name": "Test",
        "workspace": "~/.openclaw/test-workspace",
        "bot_token_env": "TEST_BOT_TOKEN",
        "probe_entrypoint": "scripts/heartbeat.py::probe",
        "expected_probes": [],
    })
    module_rel, func = spec.parse_probe_entrypoint()
    assert module_rel == "scripts/heartbeat.py"
    assert func == "probe"


def test_agent_health_spec_parse_entrypoint_rejects_malformed(types_module):
    spec = types_module.AgentHealthSpec.from_dict({
        "id": "bad",
        "display_name": "Bad",
        "workspace": "~",
        "bot_token_env": "X",
        "probe_entrypoint": "no-separator-here",
        "expected_probes": [],
    })
    with pytest.raises(ValueError, match="::"):
        spec.parse_probe_entrypoint()


def test_agent_health_spec_required_fields(types_module):
    with pytest.raises((KeyError, TypeError)):
        types_module.AgentHealthSpec.from_dict({"id": "test"})


# ─── load_fleet_manifest ─────────────────────────────────────────────


def test_load_fleet_manifest_from_valid_file(types_module, tmp_path, valid_manifest_dict):
    path = tmp_path / "fleet-manifest.json"
    path.write_text(json.dumps(valid_manifest_dict))
    manifest = types_module.load_fleet_manifest(str(path))
    assert manifest.version == 1
    assert len(manifest.agents) == 2
    assert manifest.agents[0].id == "shopping"
    assert manifest.agents[1].id == "connector"


def test_load_fleet_manifest_missing_file_raises(types_module, tmp_path):
    with pytest.raises(FileNotFoundError):
        types_module.load_fleet_manifest(str(tmp_path / "nonexistent.json"))


def test_load_fleet_manifest_invalid_json_raises(types_module, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not valid json")
    with pytest.raises(json.JSONDecodeError):
        types_module.load_fleet_manifest(str(path))


def test_load_fleet_manifest_requires_agents_array(types_module, tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"version": 1}))
    with pytest.raises((KeyError, ValueError)):
        types_module.load_fleet_manifest(str(path))


def test_load_fleet_manifest_get_agent_by_id(types_module, tmp_path, valid_manifest_dict):
    path = tmp_path / "fleet-manifest.json"
    path.write_text(json.dumps(valid_manifest_dict))
    manifest = types_module.load_fleet_manifest(str(path))
    agent = manifest.get_agent("connector")
    assert agent is not None
    assert agent.display_name == "Huckle Cat"
    assert manifest.get_agent("nonexistent") is None


# ─── ProbeResult ─────────────────────────────────────────────────────


def test_probe_result_roundtrip(types_module):
    r = types_module.ProbeResult(status="ok", detail="ttl=894s")
    d = r.to_dict()
    assert d["status"] == "ok"
    assert d["detail"] == "ttl=894s"
    back = types_module.ProbeResult.from_dict(d)
    assert back == r


def test_probe_result_without_detail(types_module):
    r = types_module.ProbeResult(status="degraded")
    d = r.to_dict()
    assert d["status"] == "degraded"


# ─── AgentProbeReport ────────────────────────────────────────────────


def test_agent_probe_report_roundtrip(types_module):
    report = types_module.AgentProbeReport(
        id="shopping",
        probe_ts="2026-04-13T16:44:58Z",
        status="ok",
        probes={
            "costco_session": types_module.ProbeResult(status="ok", detail="ttl=894s"),
            "amazon_session": types_module.ProbeResult(status="ok", detail="3d"),
        },
    )
    d = report.to_dict()
    assert d["id"] == "shopping"
    assert d["status"] == "ok"
    assert d["probes"]["costco_session"]["status"] == "ok"
    back = types_module.AgentProbeReport.from_dict(d)
    assert back.id == report.id
    assert back.probes["costco_session"].status == "ok"


def test_agent_probe_report_captures_error(types_module):
    report = types_module.AgentProbeReport(
        id="shopping",
        probe_ts="2026-04-13T16:45:00Z",
        status="error",
        probes={},
        error="docker exec timed out",
    )
    d = report.to_dict()
    assert d["error"] == "docker exec timed out"


# ─── FleetHealthReport ───────────────────────────────────────────────


def test_fleet_health_report_roundtrip(types_module):
    report = types_module.FleetHealthReport(
        generated_at="2026-04-13T16:45:00Z",
        agents={
            "shopping": types_module.AgentProbeReport(
                id="shopping", probe_ts="2026-04-13T16:44:58Z", status="ok", probes={},
            ),
        },
    )
    d = report.to_dict()
    assert d["generated_at"] == "2026-04-13T16:45:00Z"
    assert "shopping" in d["agents"]
    back = types_module.FleetHealthReport.from_dict(d)
    assert back.generated_at == report.generated_at
    assert "shopping" in back.agents


def test_fleet_health_report_empty_fleet(types_module):
    report = types_module.FleetHealthReport(
        generated_at="2026-04-13T16:45:00Z",
        agents={},
    )
    d = report.to_dict()
    assert d["agents"] == {}
