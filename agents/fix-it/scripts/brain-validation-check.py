#!/usr/bin/env python3
"""brain-validation-check.py — Mr Fixit brain-validation cron wrapper.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:brain-validation`. Subprocess-runs
~/Dropbox/openclaw-backup/scripts/validate.py and alerts on Telegram
if validation fails. Silent on success.

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


WORKSPACE = Path(os.path.expanduser("~/.openclaw/fix-it-workspace"))
CACHE_DIR = WORKSPACE / "cache"
LAST_RUN_FILE = CACHE_DIR / "last-brain-validation.json"
VALIDATE_SCRIPT = Path(
    os.path.expanduser("~/Dropbox/openclaw-backup/scripts/validate.py")
)
SUBPROCESS_TIMEOUT_S = 120
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _run_validate() -> tuple[int, str, str]:
    if not VALIDATE_SCRIPT.exists():
        return -1, "", f"validate.py not found at {VALIDATE_SCRIPT}"
    try:
        result = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -2, "", "validate.py timed out"
    except OSError as e:
        return -3, "", str(e)
    return result.returncode, result.stdout or "", result.stderr or ""


def run() -> dict:
    now = datetime.now(timezone.utc)
    rc, stdout, stderr = _run_validate()

    sent_count = 0
    status = "ok"
    if rc != 0:
        status = "ok"  # script-level failure surfaces via Telegram, not exit code
        body = (stdout + "\n" + stderr).strip() or "validate.py reported failures"
        msg = (
            "\U0001f9ea\U0001f527 Brain validation FAILED\n"
            "\n"
            f"{body[:3500]}"
        )
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        if send_message(token, chat_id, msg, silent=False):
            sent_count = 1

    result = {
        "status": status,
        "validate_rc": rc,
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
            "alert": f"\U0001f9ea\U0001f527 brain-validation-check failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
