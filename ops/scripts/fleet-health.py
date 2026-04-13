#!/usr/bin/env python3
"""fleet-health.py — central agent health orchestrator.

R3 of the registry-based health system. Reads agents/shared/fleet-
manifest.json, invokes each agent's heartbeat.py inside the openclaw
gateway container via `docker exec`, parses each result, aggregates
into a FleetHealthReport, and writes it to
~/Dropbox/openclaw-backup/fleet-health.json.

Replaces the per-agent */30 heartbeat host crons + the 3 remaining
LLM-native heartbeat crons with a single */15 host cron.

Architecture:
  manifest → loop docker exec heartbeat.py → parse stdout JSON →
  aggregate AgentProbeReport[] → FleetHealthReport →
  write fleet-health.json + emit SCRIPT_CONTRACT JSON summary.

The agent's own run() still writes its .status.md file as a side
effect during this transition (R3 ↔ R6). Once R4 (morning-status
refactor) lands, fix-it/morning-status reads fleet-health.json
instead of scraping .status.md, and R6 retires the per-agent
.status.md writes.

Conforms to agents/shared/SCRIPT_CONTRACT.md: always exits 0,
prints exactly one JSON line to stdout. Run via host cron through
ops/scripts/script-contract-host.sh so the wrapper relays any
aggregated alert to Telegram.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone


# These constants are module-level so tests can monkeypatch them.
REPO_ROOT = os.environ.get(
    "OPENCLAW_REPO_ROOT",
    os.path.expanduser("~/repo"),
)
FLEET_MANIFEST_PATH = os.path.join(REPO_ROOT, "agents", "shared", "fleet-manifest.json")
FLEET_HEALTH_OUTPUT = os.path.expanduser(
    "~/Dropbox/openclaw-backup/fleet-health.json"
)
CONTAINER_NAME = os.environ.get(
    "OPENCLAW_CONTAINER",
    "openclaw-openclaw-gateway-1",
)
PER_AGENT_TIMEOUT_S = 60


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _import_fleet_types():
    """Import fleet_health_types lazily so tests can monkeypatch
    REPO_ROOT before the import resolves the module path."""
    import importlib.util
    types_path = os.path.join(REPO_ROOT, "agents", "shared", "fleet_health_types.py")
    spec = importlib.util.spec_from_file_location("fleet_health_types", types_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_health_types"] = mod
    spec.loader.exec_module(mod)
    return mod


PROBE_AGENT_WRAPPER = "/home/node/repo/ops/scripts/probe-agent.py"


def invoke_agent_probe(spec, run_subprocess) -> dict:
    """Run one agent's probe() function inside the container and
    return a probe-shape dict. `run_subprocess` is the subprocess.run
    callable, injected so tests can stub it.

    Invocation goes through ops/scripts/probe-agent.py rather than
    calling the heartbeat.py directly. probe-agent.py imports the
    heartbeat module dynamically and calls probe() — bypassing
    run() and main(), so no .status.md side effects fire on every
    */15 orchestrator tick (R6).

    The script's stdout is one JSON line per SCRIPT_CONTRACT; we
    parse the LAST non-empty line to tolerate any logging noise.
    """
    workspace_inside_container = (
        spec.workspace.replace("~", "/home/node")
        if spec.workspace.startswith("~")
        else spec.workspace
    )
    module_rel, _func = spec.parse_probe_entrypoint()
    script_path = os.path.join(workspace_inside_container, module_rel)

    cmd = [
        "docker", "exec", CONTAINER_NAME,
        "python3", PROBE_AGENT_WRAPPER, script_path,
    ]
    try:
        proc = run_subprocess(
            cmd,
            capture_output=True,
            text=True,
            timeout=PER_AGENT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"timeout after {PER_AGENT_TIMEOUT_S}s"}
    except Exception as e:
        return {"status": "error", "error": f"docker exec failed: {e}"}

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {
            "status": "error",
            "error": f"empty stdout (returncode={proc.returncode})",
            "stderr_tail": (proc.stderr or "")[-300:],
        }

    # Parse the LAST non-empty line as JSON
    last_line = ""
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            last_line = line
    try:
        return json.loads(last_line)
    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "error": f"non-json stdout: {e}",
            "stdout_tail": stdout[-300:],
        }


def build_report(manifest, types_module, run_subprocess) -> "FleetHealthReport":
    """Iterate each agent in the manifest, run its probe, aggregate
    into a FleetHealthReport. Pure orchestration — no I/O on disk."""
    now = _utcnow_iso()
    agents: dict[str, "AgentProbeReport"] = {}

    for spec in manifest.agents:
        probe_dict = invoke_agent_probe(spec, run_subprocess)
        status = probe_dict.get("status", "error")

        # Translate the probe dict into an AgentProbeReport. The probe
        # dict's free-form fields become individual ProbeResult entries
        # so the registry has structured access. Status alone is the
        # top-level signal; details live in `probes`.
        probes: dict[str, "ProbeResult"] = {}
        for key, value in probe_dict.items():
            if key in ("status", "alert", "error", "traceback"):
                continue
            if isinstance(value, dict):
                # Nested dicts (e.g. meetings-coach `auth: {...}`) get
                # flattened into one probe per child key.
                for sub_k, sub_v in value.items():
                    probes[f"{key}.{sub_k}"] = types_module.ProbeResult(
                        status=str(sub_v) if isinstance(sub_v, str) else "ok",
                        detail=str(sub_v) if not isinstance(sub_v, str) else None,
                    )
            else:
                probes[key] = types_module.ProbeResult(
                    status="ok" if value not in ("missing", "stale", "expired") else str(value),
                    detail=str(value) if value is not None else None,
                )

        agents[spec.id] = types_module.AgentProbeReport(
            id=spec.id,
            probe_ts=now,
            status=status,
            probes=probes,
            error=probe_dict.get("error"),
            alert=probe_dict.get("alert"),
        )

    return types_module.FleetHealthReport(generated_at=now, agents=agents)


def write_report(report) -> None:
    """Write the FleetHealthReport atomically to FLEET_HEALTH_OUTPUT."""
    os.makedirs(os.path.dirname(FLEET_HEALTH_OUTPUT), exist_ok=True)
    tmp = FLEET_HEALTH_OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2)
    os.replace(tmp, FLEET_HEALTH_OUTPUT)


def summarize(report) -> dict:
    """Build the SCRIPT_CONTRACT stdout payload. Aggregates per-agent
    status into a fleet-level signal and concatenates any per-agent
    alerts into one Telegram-ready message."""
    total = len(report.agents)
    ok_count = sum(1 for r in report.agents.values() if r.status == "ok")
    degraded = [r for r in report.agents.values() if r.status not in ("ok",)]

    if not degraded:
        return {
            "status": "ok",
            "agents_checked": total,
            "agents_ok": ok_count,
        }

    alerts: list[str] = []
    for r in degraded:
        if r.alert:
            alerts.append(r.alert)
        elif r.error:
            alerts.append(f"{r.id}: {r.error}")
        else:
            alerts.append(f"{r.id}: {r.status}")

    return {
        "status": "degraded",
        "agents_checked": total,
        "agents_ok": ok_count,
        "agents_degraded": [r.id for r in degraded],
        "alert": "🦞 fleet health: " + " | ".join(alerts),
    }


def run(run_subprocess=None) -> dict:
    """Entry point. Loads manifest, runs probes, writes report,
    returns a summary dict for stdout."""
    types_module = _import_fleet_types()
    manifest = types_module.load_fleet_manifest(FLEET_MANIFEST_PATH)
    report = build_report(manifest, types_module, run_subprocess or subprocess.run)
    write_report(report)
    return summarize(report)


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"🦞 fleet-health crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
