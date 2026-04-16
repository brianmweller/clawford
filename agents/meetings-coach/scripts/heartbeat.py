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
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.google_oauth import get_credentials  # noqa: E402

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]

WORKSPACE = os.path.expanduser("~/.clawford/meetings-coach-workspace")
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
    """Returns dict of auth field -> 'ok'|'missing'|'revoked'|'error'."""
    auth = {}

    # Google — exercise the refresh round-trip, not just file existence.
    # 2026-04-15: the old "token.json exists and is JSON" check reported
    # 'ok' for 2.5 days while the refresh_token was revoked by Google.
    token_path = os.path.join(WORKSPACE, "token.json")
    creds_path = os.path.join(WORKSPACE, "credentials.json")
    if not os.path.exists(token_path):
        auth["google_auth"] = "missing"
    else:
        try:
            creds = get_credentials(creds_path, token_path, GOOGLE_SCOPES)
            auth["google_auth"] = "ok" if creds is not None else "missing"
        except FileNotFoundError:
            auth["google_auth"] = "missing"
        except Exception as exc:
            if "invalid_grant" in str(exc):
                auth["google_auth"] = "revoked"
            else:
                auth["google_auth"] = "error"

    # Workflowy — mirror workflowy-sync.get_api_key()'s fallback chain.
    # Under host-native cron the env var isn't exported, so we also
    # probe the .env files that workflowy-sync actually reads. Drift
    # between these two resolvers produced the 2026-04-15 false-positive
    # where heartbeat said "missing" while workflowy-sync ran fine.
    auth["workflowy_auth"] = "ok" if _resolve_workflowy_api_key() else "missing"

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


def _resolve_workflowy_api_key() -> str:
    """Return WORKFLOWY_API_KEY from env or the .env fallback chain used
    by workflowy-sync.get_api_key(). Empty string if not found anywhere.
    Kept structurally identical to workflowy-sync's resolver so the two
    can't drift.
    """
    key = os.environ.get("WORKFLOWY_API_KEY", "")
    if key:
        return key
    for env_file in [
        os.path.join(WORKSPACE, ".env"),
        os.path.expanduser("~/clawford/.env"),
        "/home/openclaw/clawford/.env",
        os.path.expanduser("~/.env"),
        "/tmp/.env",
    ]:
        if not os.path.exists(env_file):
            continue
        try:
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("WORKFLOWY_API_KEY=") and not line.startswith("#"):
                        candidate = line.split("=", 1)[1].strip().strip("'\"")
                        if candidate:
                            return candidate
        except OSError:
            continue
    return ""


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


def probe() -> dict:
    """Pure meetings-coach health probe. No status.md side effects.

    Fleet-health.py orchestrator (R3) calls this directly via
    docker exec to populate fleet-health.json without touching
    the per-agent .status.md files.
    """
    auth = check_auth()
    cache_ts, cache_name, cache_summary = read_cron_caches()
    missing_files = check_required_files()
    prune_stale_preps()  # housekeeping side effect on prep-*.json files

    errors: list[str] = []
    if missing_files:
        errors.append(f"missing: {', '.join(missing_files)}")

    degraded = any(v == "missing" for v in auth.values()) or bool(missing_files)
    status = "degraded" if degraded else "ok"
    error_log = "; ".join(errors) if errors else "none"

    result: dict = {
        "status": status,
        "auth": auth,
        "missing_files": missing_files,
        "last_cron_run": cache_ts,
        "last_cron_name": cache_name,
        "last_cron_result": cache_summary,
        "error_log": error_log,
    }
    if degraded:
        details = [f"{k}={v}" for k, v in auth.items() if v == "missing"]
        result["alert"] = (
            f"⚠️ meetings-coach degraded: {', '.join(details + errors) or 'missing files'}"
        )
    return result


def _write_status_md(probe_result: dict) -> None:
    """Render the probe result as the meetings-coach.status.md schema."""
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    auth = probe_result.get("auth", {})
    status = probe_result.get("status", "ok")
    last_cron_run = probe_result.get("last_cron_run") or now_str
    last_cron_name = probe_result.get("last_cron_name") or "heartbeat"
    last_cron_result = probe_result.get("last_cron_result") or "heartbeat ran"
    error_log = probe_result.get("error_log", "none")

    content = f"""# Meetings Coach — Status

- **last_heartbeat:** {now_str}
- **status:** {status}
- **last_cron_run:** {last_cron_run} — {last_cron_name}
- **last_cron_result:** {last_cron_result}
- **google_auth:** {auth.get('google_auth', 'missing')}
- **workflowy_auth:** {auth.get('workflowy_auth', 'missing')}
- **krisp_auth:** {auth.get('krisp_auth', 'missing')}
- **error_log:** {error_log}
- **token_usage_today:** —
"""
    try:
        os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        raise RuntimeError(f"status file write failed: {e}") from e


def run() -> dict:
    """Call probe() + write status.md (transition behavior)."""
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
            "alert": f"⚠️ meetings-coach heartbeat crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
