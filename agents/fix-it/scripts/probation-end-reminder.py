#!/usr/bin/env python3
"""probation-end-reminder.py — Mr Fixit probation end reminder.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:probation-end-reminder`. One-shot: reads probation.md, counts
entries in the failure log, sends a Telegram message with the count
and the verdict prompt. Does NOT propose a verdict — that's the operator's
call.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import re
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
LAST_RUN_FILE = CACHE_DIR / "last-probation-end.json"
PROBATION_FILE = WORKSPACE / "probation.md"

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

_FAILURE_LINE_RE = re.compile(
    r"^\s*-\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\|\s*P\d+\s*\|"
)


def count_failures(text: str) -> int:
    """Count failure log entries — only lines inside the `## Failure
    log` section that match the `- YYYY-MM-DD HH:MM | Pn | ...` shape.
    Lines elsewhere in the document are ignored even if they match."""
    in_failure_log = False
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_failure_log = stripped.lower().startswith("## failure log")
            continue
        if in_failure_log and _FAILURE_LINE_RE.match(line):
            count += 1
    return count


def format_message(failure_count: int) -> str:
    return (
        f"\U0001f9ea\U0001f527 Probation ends today.\n"
        f"Failures logged: {failure_count}.\n"
        f"Verdict requested: keep / extend / retire.\n"
        f"Retirement command: bash ~/repo/agents/fix-it/retire.sh\n"
    )


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now = datetime.now(timezone.utc)

    if not PROBATION_FILE.exists():
        result = {
            "status": "degraded",
            "alert": f"probation.md not found at {PROBATION_FILE}",
            "failures": 0,
            "sent": 0,
        }
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps({"timestamp": now.isoformat(), **result}, indent=2),
        )
        return result

    text = PROBATION_FILE.read_text(encoding="utf-8")
    failures = count_failures(text)
    msg = format_message(failures)

    sent_count = 0
    token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
    if send_message(token, chat_id, msg, silent=False):
        sent_count = 1

    result = {
        "status": "ok",
        "failures": failures,
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
            "alert": f"\U0001f9ea\U0001f527 probation-end-reminder failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
