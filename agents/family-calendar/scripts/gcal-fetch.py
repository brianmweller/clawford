#!/usr/bin/env python3
"""gcal-fetch.py — brain-reading shim for Mistress Mouse.

Historically this script fetched events from multiple Google Calendars
(the operator + Alex) and wrote ``cache/events-<date>.json`` with Murphy's
events filtered out via ``--skip-meetings`` against the shared brain
index. Post-2026-04-23 the full-fat shared event brain replaces both
the Google fetch and the index lookup: one writer (listener daemon +
daily rebuild), both agents read.

This script filters the brain by Mouse's owner
(``owner="mistress-mouse"``) and emits the legacy JSON shape +
writes the events-<date>.json cache file so downstream consumers
(reminder-check, tools.py fuzzy resolver, morning-briefing) don't need
to change.

Usage:
  python3 gcal-fetch.py                     # Today's events
  python3 gcal-fetch.py --date 2026-04-24   # Specific date
  python3 gcal-fetch.py --days 7            # Next N days
  python3 gcal-fetch.py --skip-meetings     # (no-op; brain always
                                             # filters by owner)
  python3 gcal-fetch.py --calendar-id ID    # (accepted for CLI-compat;
                                             # brain is multi-calendar)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.calendar_brain import (  # noqa: E402
    DEFAULT_BRAIN_FILE,
    read_brain_if_fresh,
)

WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")
CONFIG_PATH = os.path.join(WORKSPACE, "calendar-config.json")
CACHE_DIR = os.path.join(WORKSPACE, "cache")
BRAIN_FILE = os.environ.get(
    "CLAWFORD_CALENDAR_BRAIN_FILE", DEFAULT_BRAIN_FILE,
)
MAX_AGE_S = int(os.environ.get("CLAWFORD_CALENDAR_BRAIN_MAX_AGE_S", "600"))


def parse_args() -> tuple[str | None, int, str | None, bool]:
    target_date: str | None = None
    days = 1
    calendar_id: str | None = None
    skip_meetings = False
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--date" and i + 1 < len(sys.argv):
            target_date = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == "--calendar-id" and i + 1 < len(sys.argv):
            calendar_id = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--skip-meetings":
            skip_meetings = True
            i += 1
        else:
            i += 1
    return target_date, days, calendar_id, skip_meetings


def _operator_today() -> str:
    tz_name = "America/Los_Angeles"
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        tz_name = (cfg or {}).get("timezone") or tz_name
    except (OSError, json.JSONDecodeError):
        pass
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d")


def main() -> None:
    target_date, days, calendar_id, skip_meetings = parse_args()
    start = target_date or _operator_today()

    payload = read_brain_if_fresh(
        BRAIN_FILE,
        owner="mistress-mouse",
        date=start, days=days,
        max_age_seconds=MAX_AGE_S,
    )

    if payload is None:
        result = {
            "status": "error",
            "date": start,
            "days": days,
            "events": [],
            "conflicts": [],
            "errors": [{
                "source": "calendar-brain",
                "error": (
                    "brain unavailable (missing, stale, or disabled)"
                ),
            }],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "skip_meetings": skip_meetings,
            "skipped_meetings_count": 0,
            "fetched_via": "brain-unavailable",
        }
    else:
        events = payload.get("events") or []
        # Optional CLI-compat: caller can narrow to one calendar.
        if calendar_id:
            events = [
                e for e in events if e.get("calendar_id") == calendar_id
            ]
        result = {
            "status": "ok",
            "date": start,
            "days": days,
            "events": events,
            "conflicts": [],  # conflict detection moved to callers if needed
            "errors": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "skip_meetings": skip_meetings,
            "skipped_meetings_count": 0,
            "fetched_via": "brain",
            "brain_generated_at": payload.get("generated_at"),
        }

    # Preserve the legacy cache-file write so existing event-*.json
    # readers (tools.py fuzzy resolver, reminder-check) keep working.
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"events-{start}.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    import json as _contract_json
    import sys as _contract_sys
    _contract_status = "ok"
    _contract_error = None
    try:
        _contract_rc = main()
        if _contract_rc not in (0, None):
            _contract_status = "error"
            _contract_error = f"main returned {_contract_rc}"
    except SystemExit as _contract_e:
        if _contract_e.code not in (0, None):
            _contract_status = "error"
            _contract_error = f"main exited with code {_contract_e.code}"
    except BaseException as _contract_e:  # noqa: BLE001
        _contract_status = "error"
        _contract_error = str(_contract_e)[:200]
    _contract_envelope = {"status": _contract_status}
    if _contract_error:
        _contract_envelope["error"] = _contract_error
    print(_contract_json.dumps(_contract_envelope))
    _contract_sys.exit(0)
