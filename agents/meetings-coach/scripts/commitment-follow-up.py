#!/usr/bin/env python3
"""commitment-follow-up.py — Meetings Coach (Sergeant Murphy) follow-up.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`meetings-coach:commitment-follow-up`. Runs commitment-tracker.py,
formats an alert when any commitment is overdue or approaching, and
stays silent otherwise. Direct-send (not fleet path) — runs at 16 UTC
(9 AM PT), outside the morning brief window.

Pure Python templating — tracker output is already structured
overdue/approaching classification, so no LLM in the compose tier.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
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

from agents.shared.subprocess_helpers import (  # noqa: E402
    is_subprocess_error,
    run_json_script,
)
from agents.shared.telegram_api import resolve_credentials, send_message  # noqa: E402


WORKSPACE = Path(os.path.expanduser("~/.clawford/meetings-coach-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
LAST_RUN_FILE = CACHE_DIR / "last-commitment-follow-up.json"

BOT_TOKEN_ENV = "MEETINGS_BOT_TOKEN"
SUBPROCESS_TIMEOUT_S = 60


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


def _fmt_line(commitment: dict) -> str:
    to_whom = (commitment.get("to_whom") or "?").strip()
    what = (commitment.get("what") or "").strip()
    by_when = (commitment.get("by_when") or "").strip()
    days_info = (commitment.get("days_info") or "").strip()
    when_part = f" (due {by_when}" if by_when else ""
    if days_info:
        when_part += f", {days_info}" if when_part else f" ({days_info}"
    if when_part:
        when_part += ")"
    return f"  \u2022 {to_whom} \u2192 {what}{when_part}"


def format_message(tracker: dict) -> str | None:
    """Render the commitment follow-up alert. Returns None when there
    is nothing actionable (no overdue or approaching items) — the cron
    must stay silent in that case.
    """
    commitments = tracker.get("commitments") or []
    overdue = [c for c in commitments if c.get("is_overdue")]
    approaching = [c for c in commitments if c.get("is_approaching")]

    if not overdue and not approaching:
        return None

    summary = tracker.get("summary") or {}
    total = summary.get("total", len(commitments))
    overdue_count = summary.get("overdue", len(overdue))
    approaching_count = summary.get("approaching", len(approaching))

    lines: list[str] = ["\U0001f437\U0001f50d Open Items Check", ""]

    if overdue:
        lines.append("\u23f0 OVERDUE")
        for c in overdue:
            lines.append(_fmt_line(c))
        lines.append("")
    if approaching:
        lines.append("\u26a0\ufe0f APPROACHING")
        for c in approaching:
            lines.append(_fmt_line(c))
        lines.append("")

    lines.append(
        f"\U0001f4cb {total} open \u00b7 {overdue_count} overdue "
        f"\u00b7 {approaching_count} approaching"
    )
    lines.append("\U0001f437\U0001f50d")
    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)

    tracker = _run_script("commitment-tracker.py")

    # commitment-tracker is the only data source. Propagate subprocess
    # failures as a top-level error so the script-contract wrapper fires
    # a fix-it alert (2026-04-15 silent-outage class).
    if is_subprocess_error(tracker):
        error_msg = tracker["__error__"]
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "error",
                    "error": error_msg,
                    "summary": f"commitment-tracker failed: {error_msg[:120]}",
                },
                indent=2,
            ),
        )
        return {
            "status": "error",
            "error": error_msg,
            "alert": f"\U0001f437\U0001f50d commitment-follow-up failed: {error_msg[:200]}",
            "sent": 0,
        }

    if not isinstance(tracker, dict):
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "degraded",
                    "alert": "commitment-tracker.py returned no usable output",
                },
                indent=2,
            ),
        )
        return {
            "status": "degraded",
            "alert": "commitment-tracker.py returned no usable output",
            "sent": 0,
        }

    msg = format_message(tracker)
    sent_count = 0
    if msg is not None:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        if send_message(token, chat_id, msg, silent=False):
            sent_count = 1

    summary = tracker.get("summary") or {}
    _write_atomic(
        LAST_RUN_FILE,
        json.dumps(
            {
                "timestamp": now_utc.isoformat(),
                "status": "ok",
                "total": summary.get("total", 0),
                "overdue": summary.get("overdue", 0),
                "approaching": summary.get("approaching", 0),
                "sent": sent_count,
            },
            indent=2,
        ),
    )

    return {
        "status": "ok",
        "total": summary.get("total", 0),
        "overdue": summary.get("overdue", 0),
        "approaching": summary.get("approaching", 0),
        "sent": sent_count,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f437\U0001f50d commitment-follow-up failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
