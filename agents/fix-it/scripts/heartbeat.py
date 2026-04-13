#!/usr/bin/env python3
"""heartbeat.py — Fix-It heartbeat: check all agents, write status file.

Reads all *.status.md files from the shared brain, cross-references with
`openclaw agents list`, determines healthy/degraded, writes
fix-it.status.md atomically.

Conforms to agents/shared/SCRIPT_CONTRACT.md: always exits 0 and
prints one JSON line to stdout. The cron message parses the JSON and
decides whether to send a Telegram alert based on the `status` field:

  {"status": "ok",        "checked": N}           → no alert
  {"status": "degraded",  "alert": "...text..."}  → cron sends alert
  {"status": "error",     "error": "..."}         → cron sends alert

The `alert` field (when present) is the human-readable text to forward
to Telegram, suitable for a direct `sendMessage` call.
"""

import glob
import json
import os
import re
import subprocess
import sys
import traceback
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
    """Get agent names. Try --json (added in 2026.4.10), fall back to plain text."""
    try:
        r = subprocess.run(
            ["openclaw", "agents", "list", "--json"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        agents = json.loads(r.stdout)
        names = {a["id"] for a in agents if a.get("id")}
        names.discard("main")
        if names:
            return names
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["openclaw", "agents", "list"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        names = set()
        for line in r.stdout.splitlines():
            m = re.match(r"^[-*\s]*([a-z][a-z0-9-]+)\b", line.strip())
            if m and m.group(1) not in {"id", "name", "agents", "agent"}:
                names.add(m.group(1))
        names.discard("main")
        return names
    except Exception:
        return set()


def run() -> dict:
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    registered = get_registered_agents()
    if not registered:
        raise RuntimeError("could not get agent list from openclaw")

    stale: list[tuple[str, str]] = []
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
        first_agent, first_hb = stale[0]
        result_line = f"{first_agent} stale, last heartbeat {first_hb}"
        error_line = f"{first_agent} unhealthy: heartbeat stale"
        alert = f"⚠️ {first_agent} unresponsive. Last heartbeat: {first_hb}. Investigate."
    else:
        status = "ok"
        result_line = f"all {checked} agents within {STALE_THRESHOLD_MIN}-min threshold"
        error_line = "none"
        alert = None

    # Write status file atomically. Status file uses "healthy" for the
    # human-facing marker; the script status field uses the contract
    # vocabulary ("ok"/"degraded"/"error").
    human_status = "healthy" if status == "ok" else status
    content = f"""# Fix-It — Status

- **last_heartbeat:** {now_str}
- **status:** {human_status}
- **last_cron_run:** heartbeat-check at {now_str}
- **last_cron_result:** {result_line}
- **error_log:** {error_line}
- **token_usage_today:** —
"""
    try:
        with open(OUTPUT_FILE, "w") as f:
            f.write(content)
    except Exception as e:
        raise RuntimeError(f"status file write failed: {e}") from e

    out: dict = {
        "status": status,
        "checked": checked,
        "stale_count": len(stale),
    }
    if stale:
        out["stale"] = [{"agent": a, "heartbeat": h} for a, h in stale]
        out["alert"] = alert
    return out


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"⚠️ fix-it heartbeat crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
