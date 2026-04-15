#!/usr/bin/env python3
"""heartbeat.py — Family Calendar (Mistress Mouse) heartbeat probe.

Phase 4: a thin FamilyCalendarProbe subclass of
agents.shared.heartbeat_base.HeartbeatProbe. Module-level probe()/
run()/main() wrappers stay so existing callers (tests, fleet-health,
monkeypatch flows) don't need to know about the class.

Verifies required workspace files, probes Google Calendar OAuth token
freshness, prunes stale sent-reminders entries (>48h), writes
family-calendar.status.md, emits SCRIPT_CONTRACT JSON.

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
from pathlib import Path

# --- shared library sys.path shim ---
# Find the first ancestor containing agents/shared/ and prepend it to
# sys.path so `from agents.shared import X` resolves in both the local
# repo layout and the deployed <workspace>/agents/shared/ layout.
# See agents/shared/deploy.py::sync_shared_library.
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.heartbeat_base import HeartbeatProbe  # noqa: E402


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


class FamilyCalendarProbe(HeartbeatProbe):
    """Family Calendar (Mistress Mouse) heartbeat probe.

    Subclass of agents.shared.heartbeat_base.HeartbeatProbe. Delegates
    probe() to the module-level function so the existing monkeypatched
    globals (WORKSPACE, BRAIN, TOKEN_FILE, etc.) remain the single
    source of truth for tests.
    """

    AGENT_ID = "family-calendar"
    TITLE = "Family Calendar"
    EMOJI = "🐭"

    @property
    def output_file(self) -> str:
        return OUTPUT_FILE

    def probe(self) -> dict:
        return probe()

    def render_status_md(self, result: dict) -> str:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        status = result.get("status", "ok")
        last_cron_run = result.get("last_cron_run") or now_str
        last_cron_name = result.get("last_cron_name") or "heartbeat"
        last_cron_result = result.get("last_cron_result") or "heartbeat ran"
        google_auth = result.get("google_auth", "ok")
        calendars = result.get("calendars_configured", 0)

        errors: list[str] = []
        if result.get("missing_files"):
            errors.append(f"missing: {', '.join(result['missing_files'])}")
        if google_auth != "ok":
            errors.append(f"google_auth: {google_auth}")
        error_log = "; ".join(errors) if errors else "none"

        return (
            "# Family Calendar — Status\n\n"
            f"- **last_heartbeat:** {now_str}\n"
            f"- **status:** {status}\n"
            f"- **last_cron_run:** {last_cron_run} — {last_cron_name}\n"
            f"- **last_cron_result:** {last_cron_result}\n"
            f"- **calendars_configured:** {calendars}\n"
            f"- **google_auth:** {google_auth}\n"
            f"- **pruned_reminders:** {result.get('pruned_reminders', 0)}\n"
            f"- **error_log:** {error_log}\n"
        )


_default_instance = FamilyCalendarProbe()


def run() -> dict:
    """Call probe() + write family-calendar.status.md as side effect."""
    return _default_instance.run()


def main() -> int:
    return FamilyCalendarProbe().main()


if __name__ == "__main__":
    sys.exit(main())
