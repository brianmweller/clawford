"""Parsers for each input source of the obsidian-briefing generator."""

import json
import re
from pathlib import Path


def parse_morning_briefing(path: Path) -> str:
    """Parse Mistress Mouse's cached morning briefing.

    Returns cleaned text suitable for Obsidian (no Telegram formatting).
    Returns a fallback message if the file is missing.
    """
    if not path.is_file():
        return "No schedule data available."

    text = path.read_text(encoding="utf-8").strip()

    # If it's an error message from Mistress Mouse, return as-is
    if "Couldn't fetch calendars" in text:
        return text

    lines = text.split("\n")
    cleaned = []
    for line in lines:
        # Strip Telegram-specific header lines (various formats Mistress Mouse uses)
        if line.startswith("🐭📅"):
            continue
        # Strip footer lines: "🐭 N events · ..." or standalone "🐭"
        if re.match(r"^🐭\s*(\d+\s+events.*)?$", line):
            continue
        # Strip box-drawing characters
        line = line.replace("━", "")
        # Skip lines that were only box-drawing (now empty after strip)
        if line.strip() == "":
            # Keep blank lines for spacing, but only if previous wasn't blank
            if cleaned and cleaned[-1].strip() == "":
                continue
        cleaned.append(line)

    # Trim leading/trailing blank lines
    while cleaned and cleaned[0].strip() == "":
        cleaned.pop(0)
    while cleaned and cleaned[-1].strip() == "":
        cleaned.pop()

    return "\n".join(cleaned)


def parse_agent_status(fleet_health_path: Path) -> list[dict]:
    """Parse fleet-health.json into briefing-ready agent status rows.

    Post-R6, <brain>/fleet-health.json is the single authoritative source of
    per-agent health (written every 15 min by ops/scripts/fleet-health.py).
    The per-agent <agent>.status.md files were retired.

    Returns list of {name, status, last_heartbeat}. `status="ok"` is mapped
    to `"healthy"` to preserve the briefing's user-facing wording; other
    statuses (degraded, error, …) pass through unchanged. Missing or
    malformed fleet-health.json yields an empty list so the briefing
    section renders "no data" rather than crashing.
    """
    if not fleet_health_path.is_file():
        return []

    try:
        data = json.loads(fleet_health_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    agents = data.get("agents", {}) or {}
    results = []
    for agent_id, report in sorted(agents.items()):
        raw_status = report.get("status", "unknown")
        display_status = "healthy" if raw_status == "ok" else raw_status
        results.append({
            "name": agent_id,
            "status": display_status,
            "last_heartbeat": report.get("probe_ts", "—"),
        })
    return results


def _parse_entries(path: Path) -> list[dict]:
    """Parse a brain file with --- delimited entries of **key:** value fields.

    Handles the standard brain format: header + schema block quote, then
    entries separated by --- lines.
    """
    if not path.is_file():
        return []

    text = path.read_text(encoding="utf-8")

    # Split on --- dividers
    blocks = re.split(r"\n---\s*\n", text)

    entries = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        # Skip the header / schema block (starts with # or >)
        if block.startswith("#") or block.startswith(">"):
            continue

        entry = {}
        for line in block.split("\n"):
            m = re.match(r"^-\s+\*\*(\w[\w_]*):\*\*\s*(.+)$", line)
            if m:
                key = m.group(1).strip()
                value = m.group(2).strip()
                if value == "—":
                    value = None
                entry[key] = value

        if entry:
            entries.append(entry)

    return entries


def parse_commitments(path: Path) -> list[dict]:
    """Parse commitments/active.md, returning only open commitments."""
    entries = _parse_entries(path)
    return [e for e in entries if e.get("status") == "open"]


def parse_tasks(path: Path) -> list[dict]:
    """Parse tasks/queue.md, returning only open tasks."""
    entries = _parse_entries(path)
    return [e for e in entries if e.get("status") == "open"]
