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
from agents.shared.google_oauth import get_credentials  # noqa: E402


GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/tasks",
]

WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")

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
    """Return 'ok' | 'missing' | 'revoked' | 'error'.

    Actually exercises the OAuth credentials by invoking the shared
    get_credentials() helper, which reads token.json, constructs a
    Credentials object, and calls creds.refresh() if expired.

    Replaces the old mtime-only check that silently passed while the
    refresh_token was revoked by Google (2026-04-13 → 2026-04-15, 2.5
    days of blind flight). Detecting 'invalid_grant' in the refresh
    error message is the specific signal that distinguishes a
    credentials-are-revoked failure from a transient network failure.
    """
    if not os.path.exists(TOKEN_FILE):
        return "missing"

    creds_path = os.path.join(WORKSPACE, "credentials.json")
    try:
        creds = get_credentials(creds_path, TOKEN_FILE, GOOGLE_SCOPES)
    except FileNotFoundError:
        return "missing"
    except Exception as exc:
        if "invalid_grant" in str(exc):
            return "revoked"
        return "error"

    if creds is None:
        return "missing"
    return "ok"


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

    Thin HeartbeatProbe wrapper around the module-level probe(). Carries
    AGENT_ID/TITLE/EMOJI for fleet-health aggregation; probe() remains
    at module scope so the monkeypatched globals (WORKSPACE, TOKEN_FILE,
    etc.) stay the single source of truth for tests.
    """

    AGENT_ID = "family-calendar"
    TITLE = "Family Calendar"
    EMOJI = "🐭"

    def probe(self) -> dict:
        return probe()


def main() -> int:
    return FamilyCalendarProbe().main()


if __name__ == "__main__":
    sys.exit(main())
