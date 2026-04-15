"""Tests for ops/scripts/fleet-health.py.

R3 of the registry-based health system. Verifies the orchestrator:
  - reads fleet-manifest.json correctly
  - invokes each agent's heartbeat.py as a host subprocess (mocked)
  - parses each probe result
  - aggregates into FleetHealthReport
  - writes fleet-health.json with the expected schema
  - emits SCRIPT_CONTRACT stdout summary that the host wrapper relays

Phase 6.5: the invocation path used to go through `docker exec` into
the openclaw gateway container. Post-6.5 it's a bare host subprocess
via /usr/bin/python3. The mocks no longer see "docker" in the cmd
list.

Run: cd ops/scripts && python3 -m pytest test_fleet_health.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent


def _load_orchestrator():
    """Load fleet-health.py via importlib (hyphen in filename)."""
    path = SCRIPTS_DIR / "fleet-health.py"
    spec = importlib.util.spec_from_file_location("fleet_health_orch", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_health_orch"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Build a fake repo layout with fleet-manifest.json + fleet_health_types.py
    + an output dir, and point fleet-health.py at it."""
    repo = tmp_path / "repo"
    (repo / "agents" / "shared").mkdir(parents=True)

    # Copy the real fleet_health_types.py + fleet-manifest.json so the
    # orchestrator's _import_fleet_types() works.
    real_shared = Path(__file__).resolve().parent.parent.parent / "agents" / "shared"
    (repo / "agents" / "shared" / "fleet_health_types.py").write_text(
        (real_shared / "fleet_health_types.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    # Minimal manifest with 2 agents (shopping + connector) for fast tests
    manifest = {
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
    (repo / "agents" / "shared" / "fleet-manifest.json").write_text(json.dumps(manifest))

    output = tmp_path / "openclaw-backup" / "fleet-health.json"

    orch = _load_orchestrator()
    monkeypatch.setattr(orch, "REPO_ROOT", str(repo))
    monkeypatch.setattr(orch, "FLEET_MANIFEST_PATH", str(repo / "agents" / "shared" / "fleet-manifest.json"))
    monkeypatch.setattr(orch, "FLEET_HEALTH_OUTPUT", str(output))

    return orch, output


def _fake_proc(stdout: str, returncode: int = 0, stderr: str = ""):
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


# ─── happy path: all agents return ok ────────────────────────────────


def test_run_all_agents_ok_writes_clean_report(fake_repo):
    orch, output = fake_repo
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "shopping" in " ".join(cmd):
            return _fake_proc(json.dumps({
                "status": "ok",
                "costco_session": "ok",
                "amazon_session": "ok",
                "pending_actions": 0,
            }))
        if "connector" in " ".join(cmd):
            return _fake_proc(json.dumps({
                "status": "ok",
                "missing_files": [],
                "pruned_triage": 0,
            }))
        return _fake_proc("", returncode=1)

    summary = orch.run(run_subprocess=fake_run)

    assert summary["status"] == "ok"
    assert summary["agents_checked"] == 2
    assert summary["agents_ok"] == 2
    assert "alert" not in summary

    # fleet-health.json must exist with both agents
    assert output.exists()
    report = json.loads(output.read_text())
    assert "shopping" in report["agents"]
    assert "connector" in report["agents"]
    assert report["agents"]["shopping"]["status"] == "ok"

    # 2 bare-host python3 invocations (Phase 6.5 — no docker exec)
    assert len(calls) == 2
    assert "docker" not in calls[0]
    assert any("python3" in part for part in calls[0]), calls[0]
    assert any("probe-agent.py" in part for part in calls[0]), calls[0]


# ─── one agent degraded ──────────────────────────────────────────────


def test_run_aggregates_alerts_when_agent_degraded(fake_repo):
    orch, output = fake_repo

    def fake_run(cmd, **kwargs):
        if "shopping" in " ".join(cmd):
            return _fake_proc(json.dumps({
                "status": "degraded",
                "costco_session": "expired",
                "amazon_session": "ok",
                "alert": "⚠️ shopping degraded: costco JWT expired",
            }))
        return _fake_proc(json.dumps({"status": "ok", "missing_files": []}))

    summary = orch.run(run_subprocess=fake_run)

    assert summary["status"] == "degraded"
    assert summary["agents_checked"] == 2
    assert summary["agents_ok"] == 1
    assert "shopping" in summary["agents_degraded"]
    assert "alert" in summary
    assert "shopping degraded" in summary["alert"]


# ─── timeout per agent ───────────────────────────────────────────────


def test_run_handles_per_agent_timeout(fake_repo):
    orch, output = fake_repo

    def fake_run(cmd, **kwargs):
        if "shopping" in " ".join(cmd):
            raise subprocess.TimeoutExpired(cmd, 60)
        return _fake_proc(json.dumps({"status": "ok"}))

    summary = orch.run(run_subprocess=fake_run)
    assert summary["status"] == "degraded"

    report = json.loads(output.read_text())
    assert report["agents"]["shopping"]["status"] == "error"
    assert "timeout" in report["agents"]["shopping"]["error"]
    # connector should still be probed and ok
    assert report["agents"]["connector"]["status"] == "ok"


# ─── malformed stdout ────────────────────────────────────────────────


def test_run_handles_non_json_stdout(fake_repo):
    orch, output = fake_repo

    def fake_run(cmd, **kwargs):
        if "shopping" in " ".join(cmd):
            return _fake_proc("Some random text not JSON")
        return _fake_proc(json.dumps({"status": "ok"}))

    summary = orch.run(run_subprocess=fake_run)
    assert summary["status"] == "degraded"
    report = json.loads(output.read_text())
    assert report["agents"]["shopping"]["status"] == "error"
    assert "non-json" in report["agents"]["shopping"]["error"]


def test_run_parses_last_line_of_multiline_stdout(fake_repo):
    """heartbeat scripts may print log noise to stdout before the
    JSON payload. The orchestrator must use the LAST non-empty line."""
    orch, output = fake_repo

    def fake_run(cmd, **kwargs):
        return _fake_proc(
            "log line 1\n"
            "[heartbeat] some debug noise\n"
            + json.dumps({"status": "ok", "noted": True})
            + "\n"
        )

    summary = orch.run(run_subprocess=fake_run)
    assert summary["status"] == "ok"
    report = json.loads(output.read_text())
    assert all(a["status"] == "ok" for a in report["agents"].values())


# ─── fleet-health.json schema ────────────────────────────────────────


def test_fleet_health_json_has_generated_at_and_agents(fake_repo):
    orch, output = fake_repo

    def fake_run(cmd, **kwargs):
        return _fake_proc(json.dumps({"status": "ok"}))

    orch.run(run_subprocess=fake_run)
    report = json.loads(output.read_text())
    assert "generated_at" in report
    assert "agents" in report
    assert isinstance(report["agents"], dict)


def test_probes_dict_includes_per_agent_fields(fake_repo):
    orch, output = fake_repo

    def fake_run(cmd, **kwargs):
        if "shopping" in " ".join(cmd):
            return _fake_proc(json.dumps({
                "status": "ok",
                "costco_session": "ok",
                "amazon_session": "ok",
                "pending_actions": 0,
            }))
        return _fake_proc(json.dumps({
            "status": "ok",
            "missing_files": [],
            "pruned_triage": 0,
        }))

    orch.run(run_subprocess=fake_run)
    report = json.loads(output.read_text())
    shopping_probes = report["agents"]["shopping"]["probes"]
    assert "costco_session" in shopping_probes
    assert "amazon_session" in shopping_probes
    connector_probes = report["agents"]["connector"]["probes"]
    assert "pruned_triage" in connector_probes


def test_main_always_exits_zero_and_prints_one_json_line(fake_repo, capsys):
    orch, output = fake_repo
    # Force run() to crash by pointing at a bogus manifest
    orch.FLEET_MANIFEST_PATH = "/nonexistent/manifest.json"
    rc = orch.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "error"
