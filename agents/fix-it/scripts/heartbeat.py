#!/usr/bin/env python3
"""heartbeat.py — Fix-It probe: monitor fleet-health.json freshness.

R4+ rewrite. Fix-it's probe used to scrape per-agent .status.md
files under ~/Dropbox/openclaw-backup/agents/*.status.md. That
pathway was orphaned by R3+R6 — fleet-health.py now calls each
agent's probe() via probe-agent.py, bypassing run() so no
.status.md side effects fire. Scraping that stale directory
produced false-positive "agent unresponsive" alerts.

New responsibility: fix-it's probe monitors the ONE thing
fleet-health.py cannot report about itself — that fleet-health.py
is actually running and writing fleet-health.json on schedule.
Cross-agent alerting (connector degraded, shopping error, …)
is already handled by fleet-health.py's summarize(), so fix-it's
probe intentionally does NOT duplicate per-agent alert text.

Contract:
  probe() → {status, checked, stale_count, last_cron_*, error_log,
             [alert]}          — pure, no side effects
  run()   → probe() + _write_status_md() as a side effect (kept
             for standalone invocation + for the installed
             fix-it.status.md artifact; fleet-health.py calls
             probe() directly through probe-agent.py)
  main()  → SCRIPT_CONTRACT wrapper, one JSON line to stdout

Conforms to agents/shared/SCRIPT_CONTRACT.md.
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timedelta, timezone

BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
STATUS_DIR = os.path.join(BRAIN, "agents")
OUTPUT_FILE = os.path.join(STATUS_DIR, "fix-it.status.md")
FLEET_HEALTH_PATH = os.path.join(BRAIN, "fleet-health.json")

# fleet-health.py runs every 15 min. 30 min ≈ 2 missed runs before we alarm.
FLEET_HEALTH_STALE_MIN = 30


def _parse_generated_at(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def probe() -> dict:
    """Pure fix-it health probe. Reads fleet-health.json, reports on
    its freshness. Returns a structured dict — no .status.md write.

    Called directly by fleet-health.py via probe-agent.py during the
    */15 orchestrator tick; also invoked by run() when fix-it's
    heartbeat.py is run standalone.
    """
    now = datetime.now(timezone.utc)

    if not os.path.exists(FLEET_HEALTH_PATH):
        return {
            "status": "error",
            "checked": 0,
            "stale_count": 0,
            "last_cron_run": None,
            "last_cron_name": "heartbeat-check",
            "last_cron_result": "fleet-health.json missing",
            "error_log": "fleet-health.json missing",
            "alert": (
                "⚠️ fleet-health.json missing at "
                f"{FLEET_HEALTH_PATH}. fleet-health-host.sh may be broken."
            ),
        }

    try:
        with open(FLEET_HEALTH_PATH, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {
            "status": "error",
            "checked": 0,
            "stale_count": 0,
            "last_cron_run": None,
            "last_cron_name": "heartbeat-check",
            "last_cron_result": f"fleet-health.json unreadable: {e}",
            "error_log": f"fleet-health.json unreadable: {e}",
            "alert": f"⚠️ fleet-health.json unreadable: {e}",
        }

    generated_at_raw = report.get("generated_at", "")
    generated_at = _parse_generated_at(generated_at_raw)
    agents = report.get("agents", {}) or {}
    checked = len(agents)
    stale_count = sum(
        1 for a in agents.values()
        if isinstance(a, dict) and a.get("status") not in ("ok",)
    )

    if generated_at is None:
        return {
            "status": "error",
            "checked": checked,
            "stale_count": stale_count,
            "last_cron_run": None,
            "last_cron_name": "heartbeat-check",
            "last_cron_result": "fleet-health.json has no parseable generated_at",
            "error_log": "unparseable generated_at",
            "alert": "⚠️ fleet-health.json has no parseable generated_at.",
        }

    age = now - generated_at
    if age > timedelta(minutes=FLEET_HEALTH_STALE_MIN):
        age_min = int(age.total_seconds() // 60)
        return {
            "status": "error",
            "checked": checked,
            "stale_count": stale_count,
            "last_cron_run": generated_at_raw,
            "last_cron_name": "heartbeat-check",
            "last_cron_result": f"fleet-health.json stale ({age_min} min old)",
            "error_log": f"fleet-health.json stale ({age_min} min old)",
            "alert": (
                f"⚠️ fleet-health.json stale — last generated {age_min} min ago "
                f"(> {FLEET_HEALTH_STALE_MIN} min threshold). "
                "fleet-health-host.sh may be broken."
            ),
        }

    result_line = (
        f"fleet-health fresh, {checked} agents "
        f"({checked - stale_count} ok, {stale_count} non-ok)"
    )
    return {
        "status": "ok",
        "checked": checked,
        "stale_count": stale_count,
        "last_cron_run": generated_at_raw,
        "last_cron_name": "heartbeat-check",
        "last_cron_result": result_line,
        "error_log": "none",
    }


def _write_status_md(probe_result: dict) -> None:
    """Render the probe result as fix-it.status.md. Kept for
    standalone invocation — fleet-health.py no longer triggers this
    path (it calls probe() via probe-agent.py)."""
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    status = probe_result.get("status", "ok")
    human_status = "healthy" if status == "ok" else status
    result_line = probe_result.get("last_cron_result", "")
    error_line = probe_result.get("error_log", "none")

    content = f"""# Fix-It — Status

- **last_heartbeat:** {now_str}
- **status:** {human_status}
- **last_cron_run:** heartbeat-check at {now_str}
- **last_cron_result:** {result_line}
- **error_log:** {error_line}
- **token_usage_today:** —
"""
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        raise RuntimeError(f"status file write failed: {e}") from e


def run() -> dict:
    """Call probe() + write status.md (standalone path)."""
    result = probe()
    _write_status_md(result)
    return result


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
    raise SystemExit(main())
