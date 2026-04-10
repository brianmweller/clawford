#!/usr/bin/env python3
"""Validate the shared brain directory structure and file formats.

Checks directories, agent status files, commitments, tasks, facts, and people
files for structural integrity. Prints a summary and exits 0 on pass, 1 on
any critical failure.

Usage:
    python3 validate.py [BRAIN_ROOT]

BRAIN_ROOT defaults to ~/Dropbox/openclaw-backup/.
"""

import re
import sys
from pathlib import Path

# ── Configuration ───────────────────────────────────────────────

REQUIRED_DIRS = ["agents", "people", "facts", "commitments", "tasks", "notes"]

STATUS_REQUIRED_FIELDS = {"last_heartbeat", "status"}

COMMITMENT_REQUIRED_FIELDS = {"id", "who", "what", "status", "source_agent", "created_at"}
COMMITMENT_STATUSES = {"open", "completed", "overdue", "cancelled"}

TASK_REQUIRED_FIELDS = {"id", "description", "status", "source_agent", "created_at"}
TASK_STATUSES = {"open", "done", "cancelled"}

FACT_REQUIRED_FIELDS = {"id", "content", "subject", "confidence", "category", "recorded_at"}
FACT_CATEGORIES = {"identity", "established", "situation", "preference", "plan", "logistics", "rumor"}

PEOPLE_REQUIRED_FIELDS = {"slug"}

ID_PATTERN = re.compile(r"^[\w-]+-\d{4}-\d{2}-\d{2}-\d+$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_FILE_PATTERN = re.compile(r"^\d{4}-\d{2}\.md$")
FIELD_RE = re.compile(r"^-\s+\*\*(\w[\w_]*):\*\*\s*(.+)$")


# ── Parsing (same logic as obsidian-briefing/parsers.py) ────────

def parse_entries(path: Path) -> list[dict]:
    """Parse a brain file with --- delimited entries of **key:** value fields."""
    if not path.is_file():
        return []

    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n---\s*\n", text)

    entries = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if block.startswith("#") or block.startswith(">"):
            continue

        entry = {}
        for line in block.split("\n"):
            m = FIELD_RE.match(line)
            if m:
                key = m.group(1).strip()
                value = m.group(2).strip()
                if value == "\u2014":  # em dash
                    value = None
                entry[key] = value

        if entry:
            entries.append(entry)

    return entries


def parse_status_fields(path: Path) -> dict:
    """Parse the first 8 lines of a status file for **key:** value pairs."""
    fields = {}
    lines = path.read_text(encoding="utf-8").split("\n")[:8]
    for line in lines:
        m = FIELD_RE.match(line)
        if m:
            key = m.group(1).strip()
            value = m.group(2).strip()
            if value == "\u2014":
                value = None
            fields[key] = value
    return fields


# ── Validators ──────────────────────────────────────────────────

class Results:
    def __init__(self):
        self.passes = 0
        self.warnings = 0
        self.failures = 0

    def passed(self, msg: str):
        self.passes += 1
        print(f"PASS  {msg}")

    def warn(self, msg: str):
        self.warnings += 1
        print(f"WARN  {msg}", file=sys.stderr)

    def fail(self, msg: str):
        self.failures += 1
        print(f"FAIL  {msg}", file=sys.stderr)


def check_directories(brain: Path, r: Results):
    present = sum(1 for d in REQUIRED_DIRS if (brain / d).is_dir())
    if present == len(REQUIRED_DIRS):
        r.passed(f"directories: {present}/{len(REQUIRED_DIRS)} required directories present")
    else:
        missing = [d for d in REQUIRED_DIRS if not (brain / d).is_dir()]
        r.fail(f"directories: missing {', '.join(missing)}")


def check_agents(brain: Path, r: Results):
    agents_dir = brain / "agents"
    if not agents_dir.is_dir():
        return

    status_files = sorted(agents_dir.glob("*.status.md"))
    if not status_files:
        r.warn("agents: no status files found")
        return

    errors = 0
    for f in status_files:
        fields = parse_status_fields(f)
        missing = STATUS_REQUIRED_FIELDS - set(fields.keys())
        if missing:
            r.warn(f"agents: {f.name} missing field(s) {', '.join(sorted(missing))}")
            errors += 1

    if errors == 0:
        r.passed(f"agents: {len(status_files)} status file(s), 0 errors")


def check_entries(
    brain: Path,
    r: Results,
    label: str,
    rel_path: str,
    required_fields: set,
    valid_statuses: set | None = None,
):
    path = brain / rel_path
    if not path.is_file():
        r.fail(f"{label}: {rel_path} not found")
        return

    entries = parse_entries(path)
    errors = 0
    seen_ids = set()

    for entry in entries:
        entry_id = entry.get("id", "<no id>")

        # Required fields
        missing = required_fields - set(entry.keys())
        if missing:
            r.warn(f"{label}: entry {entry_id} missing field(s) {', '.join(sorted(missing))}")
            errors += 1

        # Status enum
        if valid_statuses and "status" in entry:
            if entry["status"] not in valid_statuses:
                r.warn(f"{label}: entry {entry_id} invalid status '{entry['status']}'")
                errors += 1

        # ID format
        if "id" in entry and entry["id"] and not ID_PATTERN.match(entry["id"]):
            r.warn(f"{label}: entry {entry_id} ID doesn't match expected pattern")
            errors += 1

        # Duplicate IDs
        if "id" in entry and entry["id"]:
            if entry["id"] in seen_ids:
                r.warn(f"{label}: duplicate ID {entry_id}")
                errors += 1
            seen_ids.add(entry["id"])

        # Date fields
        for date_field in ("by_when", "due_date"):
            val = entry.get(date_field)
            if val and not DATE_PATTERN.match(val):
                r.warn(f"{label}: entry {entry_id} field {date_field} not YYYY-MM-DD: '{val}'")
                errors += 1

    if errors == 0:
        r.passed(f"{label}: {len(entries)} entries, 0 errors")


def check_facts(brain: Path, r: Results):
    facts_dir = brain / "facts"
    if not facts_dir.is_dir():
        return

    fact_files = sorted(facts_dir.glob("*.md"))
    if not fact_files:
        r.passed("facts: no fact files found (ok if new brain)")
        return

    total_entries = 0
    errors = 0

    for f in fact_files:
        # Filename format
        if not MONTH_FILE_PATTERN.match(f.name):
            r.warn(f"facts: {f.name} doesn't match YYYY-MM.md pattern")
            errors += 1

        entries = parse_entries(f)
        total_entries += len(entries)

        for entry in entries:
            entry_id = entry.get("id", "<no id>")

            missing = FACT_REQUIRED_FIELDS - set(entry.keys())
            if missing:
                r.warn(f"facts: entry {entry_id} in {f.name} missing field(s) {', '.join(sorted(missing))}")
                errors += 1

            # Category enum
            cat = entry.get("category")
            if cat and cat not in FACT_CATEGORIES:
                r.warn(f"facts: entry {entry_id} invalid category '{cat}'")
                errors += 1

            # Confidence float
            conf = entry.get("confidence")
            if conf:
                try:
                    c = float(conf)
                    if not (0.0 <= c <= 1.0):
                        r.warn(f"facts: entry {entry_id} confidence {conf} not in [0.0, 1.0]")
                        errors += 1
                except ValueError:
                    r.warn(f"facts: entry {entry_id} confidence '{conf}' not a valid float")
                    errors += 1

    if errors == 0:
        r.passed(f"facts: {total_entries} entries across {len(fact_files)} file(s), 0 errors")


def check_people(brain: Path, r: Results):
    people_dir = brain / "people"
    if not people_dir.is_dir():
        return

    people_files = [f for f in sorted(people_dir.glob("*.md")) if f.name != "_template.md"]
    if not people_files:
        r.passed("people: no people files found (ok if new brain)")
        return

    errors = 0
    for f in people_files:
        fields = parse_status_fields(f)  # same first-N-lines parsing works
        missing = PEOPLE_REQUIRED_FIELDS - set(fields.keys())
        if missing:
            r.warn(f"people: {f.name} missing field(s) {', '.join(sorted(missing))}")
            errors += 1

    if errors == 0:
        r.passed(f"people: {len(people_files)} file(s), 0 errors")


# ── Main ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        brain = Path(sys.argv[1])
    else:
        brain = Path.home() / "Dropbox" / "openclaw-backup"

    if not brain.is_dir():
        print(f"FAIL  brain root not found: {brain}", file=sys.stderr)
        sys.exit(1)

    r = Results()

    check_directories(brain, r)
    check_agents(brain, r)
    check_entries(brain, r, "commitments", "commitments/active.md",
                  COMMITMENT_REQUIRED_FIELDS, COMMITMENT_STATUSES)
    check_entries(brain, r, "tasks", "tasks/queue.md",
                  TASK_REQUIRED_FIELDS, TASK_STATUSES)
    check_facts(brain, r)
    check_people(brain, r)

    print(f"\nSummary: {r.passes} PASS / {r.warnings} WARN / {r.failures} FAIL")

    sys.exit(1 if r.failures > 0 else 0)


if __name__ == "__main__":
    main()
