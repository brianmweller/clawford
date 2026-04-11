#!/usr/bin/env python3
"""Write fix-it status file from stdin. Atomic truncate-write.

Usage:
  python3 heartbeat-write.py <<'STATUSEOF'
  # Fix-It — Status
  ...
  STATUSEOF

Reads the full status snapshot from stdin and overwrites the status file.
This removes the LLM from the write path — no shell escaping, no python -c,
no approval issues.
"""

import sys

STATUS_FILE = "/home/node/Dropbox/openclaw-backup/agents/fix-it.status.md"

content = sys.stdin.read()
if not content.strip():
    print("ERROR: empty input — refusing to write empty status file", file=sys.stderr)
    sys.exit(1)

with open(STATUS_FILE, "w") as f:
    f.write(content)
