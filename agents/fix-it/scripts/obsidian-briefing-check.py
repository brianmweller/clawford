#!/usr/bin/env python3
"""obsidian-briefing-check.py — Mr Fixit obsidian-briefing cron wrapper.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:obsidian-briefing`. Subprocess-runs
~/Dropbox/openclaw-backup/scripts/obsidian-briefing/generate.py,
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


WORKSPACE = Path(os.path.expanduser("~/.openclaw/fix-it-workspace"))
CACHE_DIR = WORKSPACE / "cache"
LAST_RUN_FILE = CACHE_DIR / "last-obsidian-briefing.json"
GENERATE_SCRIPT = Path(
    os.path.expanduser("~/Dropbox/openclaw-backup/scripts/obsidian-briefing/generate.py")
)
SUBPROCESS_TIMEOUT_S = 240
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _run_generate() -> tuple[int, str]:
    if not GENERATE_SCRIPT.exists():
        return -1, f"generate.py not found at {GENERATE_SCRIPT}"
    try:
        result = subprocess.run(
            [sys.executable, str(GENERATE_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -2, "generate.py timed out"
    except OSError as e:
        return -3, str(e)
    body = (result.stderr or result.stdout or "").strip()
    return result.returncode, body


def run() -> dict:
    now = datetime.now(timezone.utc)
    rc, body = _run_generate()

    sent_count = 0
    if rc != 0:
        msg = (
            "\U0001f9ea\U0001f527 Obsidian briefing generate FAILED\n"
            "\n"
            f"{body[:3500] or '(no output)'}"
        )
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        if send_message(token, chat_id, msg, silent=False):
            sent_count = 1

    result = {
        "status": "ok" if rc == 0 else "degraded",
        "generate_rc": rc,
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
            "alert": f"\U0001f9ea\U0001f527 obsidian-briefing-check failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
