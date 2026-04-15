#!/usr/bin/env python3
"""workspace-snapshot-check.py — Mr Fixit workspace-snapshot cron wrapper.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:workspace-snapshot`. Subprocess-runs
agents/shared/workspace-snapshot.py (the fleet snapshotting tool),
silent on success, alerts on failure.

SCRIPT_CONTRACT-compliant.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.telegram_api import resolve_credentials, send_message  # noqa: E402


WORKSPACE = Path(os.path.expanduser("~/.clawford/fix-it-workspace"))
CACHE_DIR = WORKSPACE / "cache"
LAST_RUN_FILE = CACHE_DIR / "last-workspace-snapshot.json"
SNAPSHOT_SCRIPT = Path("/home/openclaw/repo/agents/shared/workspace-snapshot.py")
SUBPROCESS_TIMEOUT_S = 600
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _run_snapshot() -> tuple[int, str]:
    if not SNAPSHOT_SCRIPT.exists():
        return -1, f"workspace-snapshot.py not found at {SNAPSHOT_SCRIPT}"
    try:
        result = subprocess.run(
            [sys.executable, str(SNAPSHOT_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -2, "workspace-snapshot.py timed out"
    except OSError as e:
        return -3, str(e)
    body = (result.stderr or result.stdout or "").strip()
    return result.returncode, body


def run() -> dict:
    now = datetime.now(timezone.utc)
    rc, body = _run_snapshot()

    sent_count = 0
    if rc != 0:
        msg = (
            "\U0001f9ea\U0001f527 Workspace snapshot FAILED\n"
            "\n"
            f"{body[:3500] or '(no output)'}"
        )
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        if send_message(token, chat_id, msg, silent=False):
            sent_count = 1

    result = {
        "status": "ok" if rc == 0 else "degraded",
        "snapshot_rc": rc,
        "sent": sent_count,
    }
    _write_atomic(
        LAST_RUN_FILE,
        json.dumps({"timestamp": now.isoformat(), **result}, indent=2),
    )
    return result


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f9ea\U0001f527 workspace-snapshot-check failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
