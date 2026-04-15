#!/usr/bin/env python3
"""conflict-scan.py — Mr Fixit Dropbox conflict-copy scanner.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:conflict-scan`. Walks ~/Dropbox/openclaw-backup/ looking for
files whose names contain "conflicted copy" (Dropbox's marker for
sync conflicts). Sends a Telegram alert if any are found, silent
otherwise.

Pure Python — no LLM, no shell `find`. SCRIPT_CONTRACT-compliant.
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

from agents.shared.telegram_api import resolve_credentials, send_message  # noqa: E402


WORKSPACE = Path(os.path.expanduser("~/.openclaw/fix-it-workspace"))
CACHE_DIR = WORKSPACE / "cache"
LAST_RUN_FILE = CACHE_DIR / "last-conflict-scan.json"
SCAN_ROOT = Path(os.path.expanduser("~/Dropbox/openclaw-backup"))

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def find_conflicts(root: Path) -> list[str]:
    """Return paths (relative to root) of files whose name contains
    'conflicted copy' anywhere. Skips dotdirs to avoid Dropbox metadata."""
    if not root.exists():
        return []
    found: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "conflicted copy" in path.name.lower():
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            found.append(rel)
    return sorted(found)


def format_alert(conflicts: list[str]) -> str:
    lines: list[str] = [
        f"\U0001f9ea\U0001f527 Dropbox conflict copies: {len(conflicts)}",
        "",
    ]
    for path in conflicts[:20]:
        lines.append(f"  \u2022 {path}")
    if len(conflicts) > 20:
        lines.append(f"  \u2026 {len(conflicts) - 20} more")
    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now = datetime.now(timezone.utc)
    conflicts = find_conflicts(SCAN_ROOT)

    sent_count = 0
    if conflicts:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        msg = format_alert(conflicts)
        if send_message(token, chat_id, msg, silent=False):
            sent_count = 1

    result = {
        "status": "ok",
        "conflicts": len(conflicts),
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
            "alert": f"\U0001f9ea\U0001f527 conflict-scan failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
