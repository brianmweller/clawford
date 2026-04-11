#!/usr/bin/env python3
"""
write-status.py — Atomic status file writer for all agents.

Called by heartbeat crons to write status files without shell expansion bugs.
The LLM agent gathers state and passes fields as arguments; this script
handles the actual file write atomically.

Usage:
  python3 write-status.py <status-file-path> <json-fields>

  The JSON fields argument is a JSON object with key-value pairs that map
  to the status file template for that agent.

Example (fix-it):
  python3 write-status.py ~/Dropbox/openclaw-backup/agents/fix-it.status.md \
    '{"header":"Fix-It","last_heartbeat":"2026-04-10 14:00 UTC","status":"healthy",
      "last_cron_run":"heartbeat-check at 2026-04-10 14:00 UTC",
      "last_cron_result":"all 5 agents within 90-min threshold",
      "error_log":"none","token_usage_today":"—"}'

The script writes exactly the fields provided, in order, with the standard
markdown format. No shell expansion, no temp files, no heredocs.
"""

import json
import os
import sys


def main():
    if len(sys.argv) < 3:
        print("Usage: write-status.py <path> '<json-fields>'", file=sys.stderr)
        sys.exit(1)

    path = os.path.expanduser(sys.argv[1])
    try:
        fields = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    header = fields.pop("header", "Status")
    lines = [f"# {header} — Status", ""]
    for key, value in fields.items():
        lines.append(f"- **{key}:** {value}")

    content = "\n".join(lines) + "\n"

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    except Exception as e:
        print(f"Write failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
