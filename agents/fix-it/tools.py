"""agents/fix-it/tools.py — Mr Fixit's tool manifest for the inbox dispatcher.

Exposes three read tools on top of existing fix-it state:
  - get_fleet_health: returns the fleet-health.json snapshot summary
  - get_morning_status: returns the most recent morning status brief
  - get_known_issues: returns the operator-curated KNOWN_ISSUES.md contents

Tool executors are pure Python functions that read files and return
structured dicts / strings. No side effects.

Contract:
  TOOLS → list[dict] in OpenAI Responses tool-manifest shape
  EXECUTORS → dict[tool_name -> callable] used by tool_use.run()
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import memory_writer  # type: ignore

AGENT_ID = "fix-it"
BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
FLEET_HEALTH_PATH = os.path.join(BRAIN, "fleet-health.json")
KNOWN_ISSUES_PATH = os.path.join(BRAIN, "fix-it", "KNOWN_ISSUES.md")
MORNING_STATUS_PATH = os.path.expanduser(
    "~/.clawford/fix-it-workspace/cache/morning-brief-ready.txt"
)


def get_fleet_health() -> dict:
    """Read fleet-health.json and return a terse summary dict.

    Returns a dict with {generated_at, status, agents_ok, agents_checked,
    degraded_agents, alert}. Keys mirror the format fleet-health.py writes.
    """
    if not os.path.exists(FLEET_HEALTH_PATH):
        return {"error": "fleet-health.json missing", "path": FLEET_HEALTH_PATH}
    try:
        with open(FLEET_HEALTH_PATH, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"fleet-health.json unreadable: {exc}"}

    summary = {
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "agents_checked": report.get("agents_checked"),
        "agents_ok": report.get("agents_ok"),
        "agents_degraded": report.get("agents_degraded", []),
        "alert": report.get("alert", ""),
    }
    generated = report.get("generated_at", "")
    if generated:
        try:
            ts = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            age_min = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
            summary["age_minutes"] = age_min
        except (ValueError, AttributeError):
            pass

    per_agent = report.get("per_agent") or report.get("agents")
    if per_agent:
        summary["per_agent"] = per_agent

    return summary


def get_morning_status() -> str:
    """Read the most recent morning status brief, if one exists."""
    if not os.path.exists(MORNING_STATUS_PATH):
        return "no morning status available yet"
    try:
        return Path(MORNING_STATUS_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        return f"morning status unreadable: {exc}"


def get_known_issues() -> str:
    """Read KNOWN_ISSUES.md — the operator-curated suppression list."""
    if not os.path.exists(KNOWN_ISSUES_PATH):
        return "no KNOWN_ISSUES.md file"
    try:
        return Path(KNOWN_ISSUES_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        return f"KNOWN_ISSUES.md unreadable: {exc}"


def propose_remember(rule: str, category: str = "General") -> dict:
    """Stage a memory-write for confirmation. Auto-attaches buttons."""
    return memory_writer.propose_pending_remember(AGENT_ID, rule, category)


def confirm_remember(rule: str, category: str) -> dict:
    """Execute the staged memory-write. Called by dispatcher shortcut only."""
    return memory_writer.append_rule(AGENT_ID, rule, category)


TOOLS: list[dict] = [
    {
        "type": "function",
        "name": "get_fleet_health",
        "description": (
            "Read the current fleet-health.json snapshot. Returns a dict "
            "with overall status, per-agent health, and how old the "
            "snapshot is in minutes. Call this when the operator asks about "
            "fleet status, agent health, or what's alerting."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_morning_status",
        "description": (
            "Read the most recent morning status brief (5 AM PT / 12 UTC "
            "daily report). Returns the full markdown string. Call this "
            "when the operator asks for the morning briefing or what the overnight "
            "system state looked like."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_known_issues",
        "description": (
            "Read the operator-curated KNOWN_ISSUES.md file. This lists "
            "known-but-not-yet-fixed issues and their expiry dates — "
            "anything in this file is intentionally being ignored by Mr "
            "Fixit's alert classifier. Call this when the operator asks what's "
            "being suppressed or what's a 'known issue'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "propose_remember",
        "description": (
            "Stage a rule for the operator's confirmation, to be added to "
            "your persistent MEMORY.md. the operator will see inline buttons "
            "to remember or skip. Use when the operator says 'remember that...', "
            "'from now on...', or teaches you a new rule."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The rule to remember"},
                "category": {"type": "string", "description": "Category heading (e.g. 'Alert Classification')"},
            },
            "required": ["rule"],
        },
    },
]


EXECUTORS: dict = {
    "get_fleet_health": get_fleet_health,
    "get_morning_status": get_morning_status,
    "get_known_issues": get_known_issues,
    "propose_remember": propose_remember,
    # Shortcut-only (not in TOOLS manifest):
    "confirm_remember": confirm_remember,
}
