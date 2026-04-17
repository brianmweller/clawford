#!/usr/bin/env python3
"""heartbeat.py — Connector (Huckle Cat) heartbeat probe.

Replaces the former LLM-native heartbeat cron message with a
deterministic Python script. Verifies required workspace files,
prunes stale cache files (>14 days) and stale triage entries (>48h),
and emits SCRIPT_CONTRACT JSON on stdout.

  probe() -> dict    — pure: reads state, returns result dict.
                       Invoked by `ops/scripts/probe-agent.py` during
                       the */15 fleet-health tick; the aggregated
                       report lands in <brain>/fleet-health.json,
                       the single authoritative health source.

  main()  -> int     — SCRIPT_CONTRACT wrapper: calls probe() inside
                       try/except, prints exactly one JSON line,
                       always exits 0.

Conforms to agents/shared/SCRIPT_CONTRACT.md.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone


WORKSPACE = os.path.expanduser("~/.clawford/connector-workspace")

CONFIG_FILE = os.path.join(WORKSPACE, "connector-config.json")
TRIAGE_FILE = os.path.join(WORKSPACE, "pending-triage.json")
CACHE_DIR = os.path.join(WORKSPACE, "cache")

REQUIRED_FILES = [
    ("connector-config.json", lambda: CONFIG_FILE),
    ("pending-triage.json", lambda: TRIAGE_FILE),
]

TRIAGE_STALE_HOURS = 48
CACHE_STALE_DAYS = 14

CACHE_FILES = [
    ("last-morning-nudge.json", "morning-relationship-nudge"),
    ("last-notes-triage.json", "notes-triage"),
]


def _prune_stale_triage() -> int:
    """Remove entries from pending-triage.json whose created_at is
    older than TRIAGE_STALE_HOURS. Returns the count pruned. Tolerates
    a missing file, a non-list payload, and missing/invalid created_at
    fields — all are no-ops that return 0.
    """
    if not os.path.exists(TRIAGE_FILE):
        return 0
    try:
        with open(TRIAGE_FILE) as f:
            entries = json.load(f)
    except Exception:
        return 0
    if not isinstance(entries, list):
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=TRIAGE_STALE_HOURS)
    kept: list[dict] = []
    pruned = 0
    for entry in entries:
        created = entry.get("created_at", "") if isinstance(entry, dict) else ""
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            # Undatable entries are kept — we can't tell if they're stale
            kept.append(entry)
            continue
        if dt < cutoff:
            pruned += 1
        else:
            kept.append(entry)

    if pruned > 0:
        tmp = TRIAGE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(kept, f, indent=2)
        os.replace(tmp, TRIAGE_FILE)
    return pruned


def _prune_stale_cache() -> int:
    """Unlink files in CACHE_DIR whose mtime is older than
    CACHE_STALE_DAYS days. Returns count pruned. Tolerates missing
    cache dir."""
    if not os.path.isdir(CACHE_DIR):
        return 0
    cutoff = time.time() - (CACHE_STALE_DAYS * 86400)
    pruned = 0
    for name in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.unlink(path)
                pruned += 1
        except OSError:
            pass
    return pruned


def _read_cron_caches() -> tuple[str | None, str | None, str | None]:
    """Return (timestamp, cron_name, summary) from the freshest
    per-cron cache file. Same shape as shopping/heartbeat.py for
    consistency with fix-it/morning-status expectations."""
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
    """Pure health probe. Reads workspace state, returns a dict.

    Fleet-health.py orchestrator (R3) invokes this directly via
    `docker exec python3 -c "from heartbeat import probe; print(json.dumps(probe()))"`
    to collect all 6 agent results without side effects.
    """
    missing_files = [
        name for name, get_path in REQUIRED_FILES
        if not os.path.exists(get_path())
    ]
    pruned_triage = _prune_stale_triage()
    pruned_cache = _prune_stale_cache()
    cache_ts, cache_name, cache_summary = _read_cron_caches()

    status = "degraded" if missing_files else "ok"
    result: dict = {
        "status": status,
        "missing_files": missing_files,
        "pruned_triage": pruned_triage,
        "pruned_cache": pruned_cache,
        "last_cron_run": cache_ts,
        "last_cron_name": cache_name,
        "last_cron_result": cache_summary,
    }
    if missing_files:
        result["alert"] = (
            f"🐱 connector degraded: missing {', '.join(missing_files)}"
        )
    return result


def main() -> int:
    try:
        result = probe()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"🐱 connector heartbeat crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
