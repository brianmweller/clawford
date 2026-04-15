#!/usr/bin/env python3
"""gmail-invite-alert.py — Family Calendar (Mistress Mouse) invite alert.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`family-calendar:gmail-invite-check`. Runs the existing I/O script
`gmail-invite-check.py` as a subprocess, formats each new invite as
a Telegram alert (pure Python string templating — no LLM needed, the
input is already structured), and sends one message per invite via
agents.shared.telegram_api.send_message.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import subprocess
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

from agents.shared.telegram_api import resolve_credentials, send_message  # noqa: E402


WORKSPACE = Path(os.path.expanduser("~/.clawford/family-calendar-workspace"))
CACHE_DIR = WORKSPACE / "cache"
SCRIPTS_DIR = WORKSPACE / "scripts"
LAST_RUN_FILE = CACHE_DIR / "last-gmail-invite.json"

PACIFIC = ZoneInfo("America/Los_Angeles")
BOT_TOKEN_ENV = "FAMILYCAL_BOT_TOKEN"
SUBPROCESS_TIMEOUT_S = 90


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name)] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    stdout = (result.stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None


def _fmt_date(iso_str: str) -> str:
    """Parse an ISO-8601 start timestamp into 'Apr 18 9:00 AM' style.
    Falls back to the raw string on parse failure."""
    if not iso_str:
        return "(unknown date)"
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(PACIFIC)
    if sys.platform != "win32":
        day_str = local.strftime("%b %-d")
    else:
        day_str = local.strftime("%b %#d")
    h12 = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{day_str} {h12}:{local.minute:02d} {suffix}"


def format_invite(invite: dict) -> str:
    """Render one invite record as a Telegram alert line.

    Shape: "🐭 New invite: {subject} on {date} from {organizer}[ at {location}]. Accept?"
    """
    subject = (invite.get("subject") or "(no subject)").strip()
    organizer = (invite.get("organizer") or "(unknown)").strip()
    date_str = _fmt_date(invite.get("start") or invite.get("received_at") or "")
    location = (invite.get("location") or "").strip()

    line = f"\U0001f42d New invite: {subject} on {date_str} from {organizer}"
    if location:
        line += f" at {location}"
    line += ". Accept?"
    return line


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)

    invites = _run_script("gmail-invite-check.py")
    if invites is None:
        invites = []

    sent = 0
    if invites:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        for invite in invites:
            if not isinstance(invite, dict):
                continue
            text = format_invite(invite)
            if send_message(token, chat_id, text, silent=False):
                sent += 1

    _write_atomic(
        LAST_RUN_FILE,
        json.dumps(
            {
                "timestamp": now_utc.isoformat(),
                "status": "ok",
                "invites_seen": len(invites) if isinstance(invites, list) else 0,
                "invites_sent": sent,
                "summary": f"{sent} invite(s) sent" if sent else "no new invites",
            },
            indent=2,
        ),
    )

    return {
        "status": "ok",
        "invites_seen": len(invites) if isinstance(invites, list) else 0,
        "invites_sent": sent,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f42d gmail-invite-alert failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
