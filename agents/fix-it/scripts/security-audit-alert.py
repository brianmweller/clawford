#!/usr/bin/env python3
"""security-audit-alert.py — Mr Fixit security-audit cron wrapper.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:security-audit`. Subprocess-runs the existing security-audit.py
script (which prints a human-formatted report to stdout) and forwards
the entire stdout to Telegram as one message.

NOTE: security-audit.py currently shells out to `openclaw security
audit --deep`, so this cron will degrade after Phase 6 removes the
openclaw binary. Phase 6 cleanup will either replace the audit
backend or retire this cron.

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
SCRIPTS_DIR = WORKSPACE / "scripts"
LAST_RUN_FILE = CACHE_DIR / "last-security-audit.json"
AUDIT_SCRIPT = SCRIPTS_DIR / "security-audit.py"
SUBPROCESS_TIMEOUT_S = 180
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _run_audit() -> tuple[int, str]:
    if not AUDIT_SCRIPT.exists():
        return -1, f"security-audit.py not found at {AUDIT_SCRIPT}"
    try:
        result = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -2, "security-audit.py timed out"
    except OSError as e:
        return -3, str(e)
    body = (result.stdout or "").strip()
    if not body:
        body = (result.stderr or "").strip() or "(no output)"
    return result.returncode, body


def run() -> dict:
    now = datetime.now(timezone.utc)
    rc, body = _run_audit()

    # security-audit is announce=true — always sends, success or failure
    msg = body[:3800] if rc == 0 else f"\U0001f9ea\U0001f527 Security audit ERROR\n\n{body[:3500]}"

    sent_count = 0
    token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
    if send_message(token, chat_id, msg, silent=False):
        sent_count = 1

    result = {
        "status": "ok" if rc == 0 else "degraded",
        "audit_rc": rc,
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
            "alert": f"\U0001f9ea\U0001f527 security-audit-alert failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
