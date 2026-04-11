#!/usr/bin/env python3
"""
heartbeat.py — Fix-It heartbeat: check all agents, write status file.

Reads all *.status.md files from the shared brain, cross-references with
`openclaw agents list`, determines healthy/degraded, writes fix-it.status.md
atomically. Outputs a JSON result for the cron to act on.

Exit codes:
  0 = healthy (cron should produce NO output)
  1 = degraded (cron should send Telegram alert with the output)
  2 = error (script bug)
"""

import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
STATUS_DIR = os.path.join(BRAIN, "agents")
OUTPUT_FILE = os.path.join(STATUS_DIR, "fix-it.status.md")
STALE_THRESHOLD_MIN = 90


def parse_timestamp(raw):
    """Parse either 'YYYY-MM-DD HH:MM UTC' or ISO-8601 with Z."""
    if not raw or raw.strip() in ("—", "-", "missing", ""):
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def get_registered_agents():
    """Get agent names from openclaw agents list."""
    try:
        r = subprocess.run(
            ["openclaw", "agents", "list"],
            capture_output=True, text=True, timeout=10,
        )
        names = set()
        for line in r.stdout.splitlines():
            m = re.match(r"^- (\S+)", line)
            if m:
                names.add(m.group(1))
        names.discard("main")
        return names
    except Exception:
        return set()


def main():
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    registered = get_registered_agents()
    if not registered:
        print(json.dumps({"error": "could not get agent list"}))
        sys.exit(2)

    stale = []
    checked = 0

    for path in sorted(glob.glob(os.path.join(STATUS_DIR, "*.status.md"))):
        agent = os.path.basename(path).replace(".status.md", "")
        if agent not in registered:
            continue

        checked += 1
        try:
            text = open(path).read()
        except Exception:
            stale.append((agent, "unreadable"))
            continue

        m = re.search(r"last_heartbeat:\*\*\s*(.+)", text)
        if not m:
            stale.append((agent, "missing"))
            continue

        dt = parse_timestamp(m.group(1))
        if dt is None:
            stale.append((agent, m.group(1).strip()))
            continue

        age = now - dt
        if age > timedelta(minutes=STALE_THRESHOLD_MIN):
            stale.append((agent, m.group(1).strip()))

    if stale:
        status = "degraded"
        agent, hb = stale[0]
        result = f"{agent} stale, last heartbeat {hb}"
        error = f"{agent} unhealthy: heartbeat stale"
    else:
        status = "healthy"
        result = f"all {checked} agents within {STALE_THRESHOLD_MIN}-min threshold"
        error = "none"

    # Write status file atomically
    content = f"""# Fix-It — Status

- **last_heartbeat:** {now_str}
- **status:** {status}
- **last_cron_run:** heartbeat-check at {now_str}
- **last_cron_result:** {result}
- **error_log:** {error}
- **token_usage_today:** —
"""
    try:
        with open(OUTPUT_FILE, "w") as f:
            f.write(content)
    except Exception as e:
        print(json.dumps({"error": f"write failed: {e}"}))
        sys.exit(2)

    # Output for the cron to act on
    out = {"status": status, "checked": checked}
    if stale:
        out["stale"] = [{"agent": a, "heartbeat": h} for a, h in stale]
        # Print alert message for cron to relay
        print(f"⚠️ {stale[0][0]} unresponsive. Last heartbeat: {stale[0][1]}. Investigate.")
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
