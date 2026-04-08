"""Parsers for each input source of the obsidian-briefing generator."""

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


def parse_agent_status(agents_dir: Path) -> list[dict]:
    """Parse all *.status.md files in a directory.

    Returns list of dicts with keys: name, status, last_heartbeat.
    Only reads the first 8 lines of each file (structured fields).
    """
    results = []
    status_files = sorted(agents_dir.glob("*.status.md"))

    for f in status_files:
        agent_name = f.stem.replace(".status", "")
        entry = {"name": agent_name, "status": "unknown", "last_heartbeat": "—"}

        lines = f.read_text(encoding="utf-8").split("\n")[:8]
        for line in lines:
            m = re.match(r"^-\s+\*\*(\w[\w_]*):\*\*\s*(.+)$", line)
            if m:
                key = m.group(1).strip()
                value = m.group(2).strip()
                if key == "status":
                    entry["status"] = value
                elif key == "last_heartbeat":
                    entry["last_heartbeat"] = value

        results.append(entry)

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
