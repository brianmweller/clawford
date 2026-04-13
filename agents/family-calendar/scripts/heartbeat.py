#!/usr/bin/env python3
"""heartbeat.py — Family Calendar (Mistress Mouse) heartbeat probe.

Replaces the former LLM-native heartbeat cron with a deterministic
Python script. Verifies required workspace files, probes Google
Calendar OAuth token freshness, prunes stale sent-reminders entries
(>48h), writes family-calendar.status.md, emits SCRIPT_CONTRACT JSON.

Two-layer design matching connector/heartbeat.py:

  probe() -> dict    — pure: reads state, returns result dict.
                       Fleet-health.py orchestrator (R3) calls this
                       directly via docker exec.
  run()   -> dict    — calls probe() + writes status.md (transition).
  main()  -> int     — SCRIPT_CONTRACT wrapper (always exit 0).

Reuses the 48h prune threshold from reminder-check.py::prune_old_reminders
for consistency — the two scripts must agree on what "stale" means or
they'll fight over sent-reminders.json on alternating cron ticks.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone


WORKSPACE = os.path.expanduser("~/.openclaw/family-calendar-workspace")
BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
OUTPUT_FILE = os.path.join(BRAIN, "agents", "family-calendar.status.md")

CONFIG_FILE = os.path.join(WORKSPACE, "calendar-config.json")
SENT_REMINDERS_FILE = os.path.join(WORKSPACE, "sent-reminders.json")
TOKEN_FILE = os.path.join(WORKSPACE, "token.json")

REMINDERS_STALE_HOURS = 48
TOKEN_STALE_DAYS = 30  # Any gcal-fetch call should touch token.json;
                      # a 30+ day old mtime means the refresh flow
                      # hasn't fired (broken or idle).

CACHE_FILES = [
    ("last-morning-brief.json", "morning-briefing"),
    ("last-reminder-check.json", "reminder-check"),
    ("last-activity-email.json", "activity-email-check"),
    ("last-gmail-invite.json", "gmail-invite-check"),
    ("last-whatsapp-scan.json", "whatsapp-chat-scan"),
]


def _required_files() -> dict:
    return {
        "calendar-config.json": CONFIG_FILE,
        "sent-reminders.json": SENT_REMINDERS_FILE,
    }


def _check_google_auth() -> str:
    """Return 'ok' | 'stale' | 'missing'.

    Does NOT decode the token or probe Google — just mtime-based
    freshness, which is cheap and deterministic. The actual token
    refresh happens elsewhere (on every gcal-fetch.py invocation).
    """
    if not os.path.exists(TOKEN_FILE):
        return "missing"
    try:
        mtime = os.path.getmtime(TOKEN_FILE)
    except OSError:
        return "missing"
    age_days = (time.time() - mtime) / 86400
    return "ok" if age_days <= TOKEN_STALE_DAYS else "stale"


def _count_calendars() -> int:
    """How many calendars are configured — informational only."""
    if not os.path.exists(CONFIG_FILE):
        return 0
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        cals = data.get("calendars", [])
        return len(cals) if isinstance(cals, list) else 0
    except Exception:
        return 0


def _prune_stale_reminders() -> int:
    """Remove entries from sent-reminders.json whose ISO-8601 value
    is older than REMINDERS_STALE_HOURS. Returns the prune count.
    Tolerates missing file / malformed schema / unparseable timestamps.
    """
    if not os.path.exists(SENT_REMINDERS_FILE):
        return 0
    try:
        with open(SENT_REMINDERS_FILE) as f:
            data = json.load(f)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    reminders = data.get("reminders", {})
    if not isinstance(reminders, dict):
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=REMINDERS_STALE_HOURS)
    kept: dict[str, str] = {}
    pruned = 0
    for key, ts_str in reminders.items():
        try:
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            # Undatable entries are kept — we can't tell if they're stale
            kept[key] = ts_str
            continue
        if dt < cutoff:
            pruned += 1
        else:
            kept[key] = ts_str

    if pruned > 0:
        data["reminders"] = kept
        data["last_pruned"] = datetime.now(timezone.utc).isoformat()
        tmp = SENT_REMINDERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SENT_REMINDERS_FILE)
    return pruned


def _read_cron_caches() -> tuple[str | None, str | None, str | None]:
    latest_ts = None
    latest_name = None
    latest_summary = None
    cache_dir = os.path.join(WORKSPACE, "cache")
    for filename, name in CACHE_FILES:
        path = os.path.join(cache_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            ts_str = data.get("timestamp", "")
            if ts_str and (latest_ts is None or ts_str > latest_ts):
                latest_ts = ts_str
                latest_name = name
                latest_summary = data.get("summary", "")
        except Exception:
            continue
    return latest_ts, latest_name, latest_summary


def probe() -> dict:
    """Pure health probe. No side effects."""
    required = _required_files()
    missing_files = [name for name, path in required.items() if not os.path.exists(path)]

    google_auth = _check_google_auth()
    calendars = _count_calendars()
    pruned_reminders = _prune_stale_reminders()
    cache_ts, cache_name, cache_summary = _read_cron_caches()

    errors: list[str] = []
    if missing_files:
        errors.append(f"missing: {', '.join(missing_files)}")
    if google_auth != "ok":
        errors.append(f"google_auth: {google_auth}")

    status = "degraded" if errors else "ok"

    result: dict = {
        "status": status,
        "missing_files": missing_files,
        "google_auth": google_auth,
        "calendars_configured": calendars,
        "pruned_reminders": pruned_reminders,
        "last_cron_run": cache_ts,
        "last_cron_name": cache_name,
        "last_cron_result": cache_summary,
    }
    if errors:
        result["alert"] = f"🐭 family-calendar degraded: {'; '.join(errors)}"
    return result


def run() -> dict:
    """Call probe() + write family-calendar.status.md as side effect."""
    result = probe()
    _write_status_md(result)
    return result


def _write_status_md(probe_result: dict) -> None:
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    status = probe_result.get("status", "ok")
    last_cron_run = probe_result.get("last_cron_run") or now_str
    last_cron_name = probe_result.get("last_cron_name") or "heartbeat"
    last_cron_result = probe_result.get("last_cron_result") or "heartbeat ran"
    google_auth = probe_result.get("google_auth", "ok")
    calendars = probe_result.get("calendars_configured", 0)

    errors: list[str] = []
    if probe_result.get("missing_files"):
        errors.append(f"missing: {', '.join(probe_result['missing_files'])}")
    if google_auth != "ok":
        errors.append(f"google_auth: {google_auth}")
    error_log = "; ".join(errors) if errors else "none"

    content = f"""# Family Calendar — Status

- **last_heartbeat:** {now_str}
- **status:** {status}
- **last_cron_run:** {last_cron_run} — {last_cron_name}
- **last_cron_result:** {last_cron_result}
- **calendars_configured:** {calendars}
- **google_auth:** {google_auth}
- **pruned_reminders:** {probe_result.get("pruned_reminders", 0)}
- **error_log:** {error_log}
"""
    try:
        os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
        tmp = OUTPUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, OUTPUT_FILE)
    except Exception as e:
        raise RuntimeError(f"status file write failed: {e}") from e


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"🐭 family-calendar heartbeat crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
