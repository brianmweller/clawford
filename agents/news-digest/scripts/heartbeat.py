#!/usr/bin/env python3
"""heartbeat.py — News Digest (Lowly Worm) heartbeat probe.

Replaces the former LLM-native heartbeat cron with deterministic
Python. Verifies required state files (preferences/model.json),
checks that the persistent LinkedIn Chromium profile directory
exists, and verifies today's `ranked-<date>.json` is present after
11:00 UTC (the morning-edition cron is scheduled at 10:30 UTC).

Two-layer design matching connector + family-calendar:

  probe() -> dict    — pure, read-only. Fleet-health.py orchestrator
                       (R3) calls this via docker exec.
  run()   -> dict    — calls probe() + writes status.md.
  main()  -> int     — SCRIPT_CONTRACT wrapper (always exit 0).

The morning_edition probe is gated by UTC time-of-day: before 11:00
UTC, missing ranked-today is "pending" (fine); after 11:00, it's
"missing" (degraded). This lets heartbeat run at any time without
producing false alarms during the normal compose window.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone


WORKSPACE = os.path.expanduser("~/.openclaw/news-digest-workspace")
BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
OUTPUT_FILE = os.path.join(BRAIN, "agents", "news-digest.status.md")

LINKEDIN_PROFILE_DIR = os.path.join(WORKSPACE, "linkedin-profile")
PREFERENCES_MODEL = os.path.join(WORKSPACE, "preferences", "model.json")
CACHE_DIR = os.path.join(WORKSPACE, "cache")

# morning-edition is scheduled at 30 10 * * * (10:30 UTC). Give it
# 30 minutes of slack for compose time; after 11:00 UTC the ranked
# file should exist or something is broken.
MORNING_EDITION_DEADLINE_UTC_HOUR = 11

CACHE_FILES = [
    ("last-morning-edition.json", "morning-edition"),
    ("last-engagement-poll.json", "engagement-poll"),
    ("last-preference-update.json", "preference-update"),
    ("last-linkedin-keepalive.json", "linkedin-keepalive"),
]


def _utcnow() -> datetime:
    """Tiny wrapper so tests can monkeypatch the clock."""
    return datetime.now(timezone.utc)


def _check_linkedin_profile() -> str:
    """Return 'ok' if the persistent Chromium profile dir exists,
    'missing' otherwise. No deep content check — presence of the
    directory is proof that linkedin-auth.py has run at least once."""
    return "ok" if os.path.isdir(LINKEDIN_PROFILE_DIR) else "missing"


def _check_morning_edition() -> tuple[str, int]:
    """Return (status, item_count).

    status ∈ {'ok', 'pending', 'missing'}:
      - ok      → cache/ranked-<today>.json exists (load to count items)
      - pending → missing, but current UTC time is before the deadline
      - missing → missing, and we're past the deadline (degraded)

    item_count is the number of items in the ranked file, or 0 if not
    present / unparseable.
    """
    today = _utcnow().strftime("%Y-%m-%d")
    path = os.path.join(CACHE_DIR, f"ranked-{today}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            items = data.get("items", [])
            count = len(items) if isinstance(items, list) else 0
            return "ok", count
        except Exception:
            return "ok", 0  # file exists, just unparseable — still counts as "ok present"

    # ranked-today missing — is it too early to worry?
    now = _utcnow()
    if now.hour < MORNING_EDITION_DEADLINE_UTC_HOUR:
        return "pending", 0
    return "missing", 0


def _read_cron_caches() -> tuple[str | None, str | None, str | None]:
    latest_ts = None
    latest_name = None
    latest_summary = None
    for filename, name in CACHE_FILES:
        path = os.path.join(CACHE_DIR, filename)
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
    missing_files: list[str] = []
    if not os.path.exists(PREFERENCES_MODEL):
        missing_files.append("preferences/model.json")

    linkedin_profile = _check_linkedin_profile()
    morning_edition, items_count = _check_morning_edition()
    cache_ts, cache_name, cache_summary = _read_cron_caches()

    errors: list[str] = []
    if missing_files:
        errors.append(f"missing: {', '.join(missing_files)}")
    if linkedin_profile != "ok":
        errors.append(f"linkedin_profile: {linkedin_profile}")
    if morning_edition == "missing":
        errors.append("morning_edition: ranked-today not produced")

    status = "degraded" if errors else "ok"

    result: dict = {
        "status": status,
        "missing_files": missing_files,
        "linkedin_profile": linkedin_profile,
        "morning_edition": morning_edition,
        "items_count": items_count,
        "last_cron_run": cache_ts,
        "last_cron_name": cache_name,
        "last_cron_result": cache_summary,
    }
    if errors:
        result["alert"] = f"🐛 news-digest degraded: {'; '.join(errors)}"
    return result


def run() -> dict:
    """Call probe() + write news-digest.status.md as side effect."""
    result = probe()
    _write_status_md(result)
    return result


def _write_status_md(probe_result: dict) -> None:
    now = _utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    status = probe_result.get("status", "ok")
    last_cron_run = probe_result.get("last_cron_run") or now_str
    last_cron_name = probe_result.get("last_cron_name") or "heartbeat"
    last_cron_result = probe_result.get("last_cron_result") or "heartbeat ran"
    linkedin_profile = probe_result.get("linkedin_profile", "ok")
    morning_edition = probe_result.get("morning_edition", "ok")
    items_count = probe_result.get("items_count", 0)

    errors: list[str] = []
    if probe_result.get("missing_files"):
        errors.append(f"missing: {', '.join(probe_result['missing_files'])}")
    if linkedin_profile != "ok":
        errors.append(f"linkedin_profile: {linkedin_profile}")
    if morning_edition == "missing":
        errors.append("morning_edition: ranked-today not produced")
    error_log = "; ".join(errors) if errors else "none"

    content = f"""# News Digest — Status

- **last_heartbeat:** {now_str}
- **status:** {status}
- **last_cron_run:** {last_cron_run} — {last_cron_name}
- **last_cron_result:** {last_cron_result}
- **linkedin_profile:** {linkedin_profile}
- **morning_edition:** {morning_edition}
- **items_count:** {items_count}
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
            "alert": f"🐛 news-digest heartbeat crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
