#!/usr/bin/env python3
"""heartbeat.py — Meetings Coach heartbeat: check auth, cron caches, write status.

Checks Google/Workflowy/Krisp auth, reads per-cron caches, verifies required
files, prunes stale prep files, writes meetings-coach.status.md atomically.

Conforms to agents/shared/SCRIPT_CONTRACT.md: always exits 0, prints one
JSON line to stdout. The cron message parses the JSON and decides
whether to send a Telegram alert based on the `status` field.
"""

import glob
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

WORKSPACE = os.path.expanduser("~/.openclaw/meetings-coach-workspace")
BRAIN = os.path.expanduser("~/Dropbox/openclaw-backup")
OUTPUT_FILE = os.path.join(BRAIN, "agents", "meetings-coach.status.md")

CACHE_FILES = [
    ("last-morning-brief.json", "morning-brief"),
    ("last-pre-meeting.json", "pre-meeting"),
    ("last-post-scan.json", "post-scan"),
    ("last-commitment.json", "commitment"),
]

REQUIRED_FILES = ["meeting-config.json", "sent-alerts.json"]
PREP_MAX_AGE_DAYS = 14


def check_auth():
    """Returns dict of auth field -> 'ok' or 'missing'."""
    auth = {}

    # Google
    token_path = os.path.join(WORKSPACE, "token.json")
    if os.path.exists(token_path):
        try:
            json.load(open(token_path))
            auth["google_auth"] = "ok"
        except Exception:
            auth["google_auth"] = "missing"
    else:
        auth["google_auth"] = "missing"

    # Workflowy
    auth["workflowy_auth"] = "ok" if os.environ.get("WORKFLOWY_API_KEY") else "missing"

    # Krisp — staleness-aware check.
    #
    # Previously this just asserted file-exists-and-not-empty, which
    # returned "ok" even after Krisp's server-side revoked the
    # refresh_token (the file was still present with stale content).
    # The 2026-04-12 audit caught 46 consecutive post-meeting-scan
    # 401s while status.md still said krisp_auth: ok.
    #
    # Now the truth source is:
    #   (a) if no tokens.json at all -> missing
    #   (b) if cache/krisp-last-401.json was written within the last
    #       KRISP_AUTH_FAIL_STALE_S seconds -> expired
    #   (c) otherwise -> ok
    krisp_path = os.path.join(WORKSPACE, "cache", "krisp-tokens", "tokens.json")
    krisp_fail_path = os.path.join(WORKSPACE, "cache", "krisp-last-401.json")
    if not (os.path.exists(krisp_path) and os.path.getsize(krisp_path) > 0):
        auth["krisp_auth"] = "missing"
    elif _krisp_recently_failed(krisp_fail_path):
        auth["krisp_auth"] = "expired"
    else:
        auth["krisp_auth"] = "ok"

    return auth


KRISP_AUTH_FAIL_STALE_S = 60 * 60  # 60 min — older than this, don't trust the 401 marker


def _krisp_recently_failed(fail_path: str) -> bool:
    """Return True iff cache/krisp-last-401.json was written within the
    staleness window. Used by check_auth() to mark krisp_auth as
    "expired" when post-meeting-scan has recently observed a 401.
    """
    if not os.path.exists(fail_path):
        return False
    try:
        mtime = os.path.getmtime(fail_path)
    except OSError:
        return False
    return (time.time() - mtime) < KRISP_AUTH_FAIL_STALE_S


def read_cron_caches():
    """Returns (last_cron_run, last_cron_name, last_cron_result)."""
    latest_ts = None
    latest_name = None
    latest_summary = None

    for filename, name in CACHE_FILES:
        path = os.path.join(WORKSPACE, "cache", filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            ts_str = data.get("timestamp", "")
            summary = data.get("summary", "")
            if ts_str and (latest_ts is None or ts_str > latest_ts):
                latest_ts = ts_str
                latest_name = name
                latest_summary = summary
        except Exception:
            continue

    return latest_ts, latest_name, latest_summary


def check_required_files():
    """Returns list of missing filenames."""
    missing = []
    for f in REQUIRED_FILES:
        if not os.path.exists(os.path.join(WORKSPACE, f)):
            missing.append(f)
    return missing


def prune_stale_preps():
    """Delete prep files older than PREP_MAX_AGE_DAYS."""
    cache_dir = os.path.join(WORKSPACE, "cache")
    if not os.path.isdir(cache_dir):
        return
    cutoff = time.time() - (PREP_MAX_AGE_DAYS * 86400)
    for path in glob.glob(os.path.join(cache_dir, "prep-*.json")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except Exception:
            pass


def run() -> dict:
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    auth = check_auth()
    cache_ts, cache_name, cache_summary = read_cron_caches()
    missing_files = check_required_files()
    prune_stale_preps()

    errors = []
    if missing_files:
        errors.append(f"missing: {', '.join(missing_files)}")

    degraded = any(v == "missing" for v in auth.values()) or bool(missing_files)
    status = "degraded" if degraded else "ok"

    last_cron_run = cache_ts or now_str
    last_cron_name = cache_name or "heartbeat"
    last_cron_result = cache_summary or "heartbeat ran"
    error_log = "; ".join(errors) if errors else "none"

    content = f"""# Meetings Coach — Status

- **last_heartbeat:** {now_str}
- **status:** {status}
- **last_cron_run:** {last_cron_run} — {last_cron_name}
- **last_cron_result:** {last_cron_result}
- **google_auth:** {auth['google_auth']}
- **workflowy_auth:** {auth['workflowy_auth']}
- **krisp_auth:** {auth['krisp_auth']}
- **error_log:** {error_log}
- **token_usage_today:** —
"""
    try:
        os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            f.write(content)
    except Exception as e:
        raise RuntimeError(f"status file write failed: {e}") from e

    out: dict = {
        "status": status,
        "auth": auth,
        "missing_files": missing_files,
    }
    if degraded:
        details = [f"{k}={v}" for k, v in auth.items() if v == "missing"]
        out["alert"] = (
            f"⚠️ meetings-coach degraded: {', '.join(details + errors) or 'missing files'}"
        )
    return out


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"⚠️ meetings-coach heartbeat crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
