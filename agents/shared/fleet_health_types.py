"""fleet_health_types — schema + dataclasses for the fleet health system.

R1 of the registry-based agent health migration. Defines:

  FleetManifest         — top-level container, loaded from fleet-manifest.json
  AgentHealthSpec       — one agent's metadata + probe entrypoint
  ProbeResult           — a single probe field result (status + detail)
  AgentProbeReport      — one agent's full probe output (status + probes + error)
  FleetHealthReport     — the full snapshot written by fleet-health.py
                          to ~/Dropbox/openclaw-backup/fleet-health.json

  load_fleet_manifest(path) -> FleetManifest
                        — read + validate a JSON manifest file

No external dependencies beyond stdlib. No side effects on import.
Pure data model + one loader. Consumed by ops/scripts/fleet-health.py
(R3) and agents/fix-it/scripts/morning-status.py (R4).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentHealthSpec:
    """One agent's entry in fleet-manifest.json."""
    id: str
    display_name: str
    workspace: str            # ~-prefixed path, expanded on demand
    bot_token_env: str        # e.g. "SHOPPING_BOT_TOKEN"
    probe_entrypoint: str     # e.g. "scripts/heartbeat.py::probe"
    expected_probes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentHealthSpec:
        return cls(
            id=d["id"],
            display_name=d["display_name"],
            workspace=d["workspace"],
            bot_token_env=d["bot_token_env"],
            probe_entrypoint=d["probe_entrypoint"],
            expected_probes=list(d.get("expected_probes", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "workspace": self.workspace,
            "bot_token_env": self.bot_token_env,
            "probe_entrypoint": self.probe_entrypoint,
            "expected_probes": list(self.expected_probes),
        }

    def workspace_expanded(self) -> str:
        """Resolve ~ in workspace path."""
        return os.path.expanduser(self.workspace)

    def parse_probe_entrypoint(self) -> tuple[str, str]:
        """Split 'scripts/heartbeat.py::probe' into (module_rel_path, func_name).

        Raises ValueError if the entrypoint lacks the '::' separator.
        """
        if "::" not in self.probe_entrypoint:
            raise ValueError(
                f"probe_entrypoint must use '::' to separate module path from "
                f"function name, got: {self.probe_entrypoint!r}"
            )
        module_rel, func = self.probe_entrypoint.split("::", 1)
        return module_rel, func


@dataclass
class FleetManifest:
    """Top-level manifest loaded from fleet-manifest.json."""
    version: int
    agents: list[AgentHealthSpec]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FleetManifest:
        if "agents" not in d:
            raise ValueError("fleet-manifest.json must contain 'agents' array")
        return cls(
            version=int(d.get("version", 1)),
            agents=[AgentHealthSpec.from_dict(a) for a in d["agents"]],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "agents": [a.to_dict() for a in self.agents],
        }

    def get_agent(self, agent_id: str) -> AgentHealthSpec | None:
        for a in self.agents:
            if a.id == agent_id:
                return a
        return None


def load_fleet_manifest(path: str) -> FleetManifest:
    """Read and validate a fleet-manifest.json file.

    Raises:
        FileNotFoundError — path doesn't exist
        json.JSONDecodeError — malformed JSON
        ValueError — valid JSON but missing required fields
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return FleetManifest.from_dict(data)


@dataclass
class ProbeResult:
    """One probe field result — the shape each field in AgentProbeReport.probes
    takes. status is required; detail is free-form informational text."""
    status: str                # "ok" | "degraded" | "error" | agent-specific
    detail: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProbeResult:
        return cls(status=d["status"], detail=d.get("detail"))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


@dataclass
class AgentProbeReport:
    """One agent's full probe output, as produced by fleet-health.py."""
    id: str
    probe_ts: str                       # ISO 8601 UTC when probe() ran
    status: str                         # overall bucket: ok | degraded | error
    probes: dict[str, ProbeResult] = field(default_factory=dict)
    error: str | None = None            # populated on docker exec / probe crash
    alert: str | None = None            # human-facing alert text (from agent's probe)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentProbeReport:
        probes_raw = d.get("probes", {})
        probes = {
            k: ProbeResult.from_dict(v) if isinstance(v, dict) else ProbeResult(status=str(v))
            for k, v in probes_raw.items()
        }
        return cls(
            id=d["id"],
            probe_ts=d["probe_ts"],
            status=d["status"],
            probes=probes,
            error=d.get("error"),
            alert=d.get("alert"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "probe_ts": self.probe_ts,
            "status": self.status,
            "probes": {k: v.to_dict() for k, v in self.probes.items()},
        }
        if self.error is not None:
            out["error"] = self.error
        if self.alert is not None:
            out["alert"] = self.alert
        return out


@dataclass
class FleetHealthReport:
    """The full snapshot written by fleet-health.py every */15 min
    to ~/Dropbox/openclaw-backup/fleet-health.json."""
    generated_at: str                              # ISO 8601 UTC
    agents: dict[str, AgentProbeReport] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FleetHealthReport:
        agents_raw = d.get("agents", {})
        agents = {k: AgentProbeReport.from_dict(v) for k, v in agents_raw.items()}
        return cls(
            generated_at=d["generated_at"],
            agents=agents,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "agents": {k: v.to_dict() for k, v in self.agents.items()},
        }
