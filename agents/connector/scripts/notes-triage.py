#!/usr/bin/env python3
"""
notes-triage.py — Read and filter untriaged notes from the shared brain inbox.

Reads notes/inbox.md, parses entries, and returns untriaged notes as JSON.
The agent (LLM) does the categorization — this script just reads.

Usage:
  python3 notes-triage.py              # Untriaged notes only
  python3 notes-triage.py --all        # Include triaged notes
  python3 notes-triage.py --limit 10   # Max entries to return

Output JSON:
  {
    "untriaged": [...],
    "count": N,
    "already_triaged": N
  }
"""

import json
import os
import re
import sys

BRAIN_INBOX = os.path.expanduser("~/Dropbox/openclaw-backup/notes/inbox.md")


def parse_args():
    show_all = "--all" in sys.argv
    limit = None

    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            try:
                limit = int(sys.argv[i + 1])
            except ValueError:
                pass

    return show_all, limit


def parse_notes(filepath):
    """Parse note entries from the inbox markdown file."""
    if not os.path.exists(filepath):
        return []

    with open(filepath) as f:
        content = f.read()

    if not content.strip():
        return []

    # Split on --- dividers
    blocks = re.split(r"\n---\n", content)
    notes = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        entry = {}
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("- **") and ":**" in line:
                match = re.match(r"- \*\*(\w[\w_]*)\*\*:\s*(.*)", line)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip()
                    if value == "—" or value == "":
                        value = None
                    entry[key] = value

        if entry.get("id") or entry.get("content"):
            notes.append(entry)

    return notes


def main():
    show_all, limit = parse_args()

    notes = parse_notes(BRAIN_INBOX)

    triaged = [n for n in notes if n.get("triaged", "").lower() == "true"]
    untriaged = [n for n in notes if n.get("triaged", "").lower() != "true"]

    if show_all:
        output_notes = notes
    else:
        output_notes = untriaged

    if limit:
        output_notes = output_notes[:limit]

    result = {
        "status": "ok",
        "untriaged": output_notes if not show_all else None,
        "all": output_notes if show_all else None,
        "count": len(output_notes),
        "already_triaged": len(triaged),
        "total_in_inbox": len(notes),
    }

    # Clean up null keys
    result = {k: v for k, v in result.items() if v is not None}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
