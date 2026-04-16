#!/usr/bin/env python3
"""morning-relationship-nudge.py — Connector (Huckle Cat) morning nudge.

Phase 4 liberation: replaces the OpenClaw LLM cron
`connector:morning-relationship-nudge`. Runs people-scan.py, formats
the nudge (see format_nudge() below), and writes cache/morning-brief-ready.txt
for the 5 AM PT fleet delivery path. On Mondays the weekly-review cron is
folded in (Option C) via an additional MONDAY recap section.

Pure Python templating — people-scan output is structured, so no LLM
in the composition tier (per the operator's logic-gate rule).

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


WORKSPACE = Path(os.path.expanduser("~/.clawford/connector-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
BRIEF_FILE = CACHE_DIR / "morning-brief-ready.txt"
LAST_RUN_FILE = CACHE_DIR / "last-morning-nudge.json"

PACIFIC = ZoneInfo("America/Los_Angeles")
SUBPROCESS_TIMEOUT_S = 120


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


def _fmt_overdue_line(entry: dict) -> str:
    name = entry.get("name") or entry.get("slug") or "?"
    relationship = (entry.get("relationship") or "").strip()
    days_since = entry.get("days_since")
    channel = (entry.get("preferred_channel") or "").strip()

    rel_part = f" ({relationship})" if relationship else ""
    days_part = f"{days_since} days since last contact" if days_since is not None else "never"
    channel_part = f" · {channel}" if channel else ""
    return f"  {name}{rel_part} — {days_part}{channel_part}"


def _fmt_approaching_line(entry: dict) -> str:
    name = entry.get("name") or entry.get("slug") or "?"
    relationship = (entry.get("relationship") or "").strip()
    days_overdue = entry.get("days_overdue", 0)
    due_in = max(-days_overdue, 0)
    rel_part = f" ({relationship})" if relationship else ""
    return f"  {name}{rel_part} — due in {due_in} days"


def format_nudge(scan: dict, now_pacific: datetime) -> str:
    """Render the full morning nudge.

    `scan` is the people-scan.py JSON output. `now_pacific` is the
    current moment in Pacific time; weekday == 0 triggers the Monday
    recap fold of the retired weekly-review cron.
    """
    weekday = now_pacific.strftime("%A")
    month = now_pacific.strftime("%B")
    day = now_pacific.day
    header = f"\U0001f431\U0001f91d Relationship Check — {weekday}, {month} {day}"

    overdue = scan.get("overdue") or []
    approaching = scan.get("approaching") or []
    overdue_total = scan.get("overdue_total", len(overdue))
    summary = scan.get("summary") or {}
    tracked_total = summary.get("total", 0)

    lines: list[str] = [header, ""]

    if not overdue and not approaching:
        lines.append("Everyone's accounted for. No overdue check-ins today.")
        lines.append("")
    else:
        if overdue:
            lines.append("\U0001f44b OVERDUE")
            for entry in overdue:
                lines.append(_fmt_overdue_line(entry))
            hidden = overdue_total - len(overdue)
            if hidden > 0:
                lines.append(f"  … {hidden} more overdue not shown")
            lines.append("")
        if approaching:
            lines.append("\u23f3 APPROACHING")
            for entry in approaching:
                lines.append(_fmt_approaching_line(entry))
            lines.append("")

    if now_pacific.weekday() == 0:
        lines.append("\U0001f4c5 MONDAY RECAP")
        lines.append(f"  {tracked_total} people tracked across all circles")
        if overdue_total:
            lines.append(f"  {overdue_total} overdue going into the week")
        lines.append("")

    footer = (
        f"\U0001f431\U0001f91d {overdue_total} overdue · "
        f"{len(approaching)} approaching · {tracked_total} tracked"
    )
    lines.append(footer)

    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)
    now_pacific = now_utc.astimezone(PACIFIC)

    scan = _run_script("people-scan.py")

    # people-scan.py is the only data source for the nudge. Propagate
    # subprocess failures instead of masking them as a "no overdue
    # check-ins" brief (the 2026-04-15 silent-outage class).
    if is_subprocess_error(scan):
        error_msg = scan["__error__"]
        body = (
            f"\U0001f431\U0001f91d Couldn't check the address book this "
            f"morning — people-scan failed: {error_msg[:120]}.\n"
        )
        _write_atomic(BRIEF_FILE, body)
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "error",
                    "error": error_msg,
                    "summary": f"people-scan failed: {error_msg[:120]}",
                },
                indent=2,
            ),
        )
        return {
            "status": "error",
            "error": error_msg,
            "alert": f"\U0001f431\U0001f91d morning-relationship-nudge failed: {error_msg[:200]}",
        }

    if not isinstance(scan, dict):
        body = (
            f"\U0001f431\U0001f91d Couldn't check the address book this "
            f"morning — people-scan returned no usable output.\n"
        )
        _write_atomic(BRIEF_FILE, body)
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "degraded",
                    "alert": "people-scan.py returned no usable output",
                },
                indent=2,
            ),
        )
        return {
            "status": "degraded",
            "alert": "\U0001f431\U0001f91d people-scan.py returned no usable output",
        }

    body = format_nudge(scan, now_pacific)
    _write_atomic(BRIEF_FILE, body)

    summary = scan.get("summary") or {}
    is_monday = now_pacific.weekday() == 0

    _write_atomic(
        LAST_RUN_FILE,
        json.dumps(
            {
                "timestamp": now_utc.isoformat(),
                "status": "ok",
                "overdue_total": scan.get("overdue_total", 0),
                "approaching_count": len(scan.get("approaching") or []),
                "tracked_total": summary.get("total", 0),
                "monday_recap_included": is_monday,
            },
            indent=2,
        ),
    )

    return {
        "status": "ok",
        "brief_path": str(BRIEF_FILE),
        "overdue_total": scan.get("overdue_total", 0),
        "approaching_count": len(scan.get("approaching") or []),
        "tracked_total": summary.get("total", 0),
        "monday_recap_included": is_monday,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f431\U0001f91d morning-relationship-nudge failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
