#!/usr/bin/env python3
"""file-size-monitor.py — Mr Fixit large-file monitor.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:file-size-monitor`. Walks ~/Dropbox/openclaw-backup/ for
files > 500 KB and alerts on Telegram if any are found. Helps catch
runaway log files / cache bloat before it pollutes the brain.

SCRIPT_CONTRACT-compliant.
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
LAST_RUN_FILE = CACHE_DIR / "last-file-size-monitor.json"
SCAN_ROOT = Path(os.path.expanduser("~/Dropbox/openclaw-backup"))
SIZE_THRESHOLD_BYTES = 500 * 1024  # 500 KB

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

# Directories whose contents Mr Fixit deliberately ignores. Mostly
# legitimately-large infrastructure (snapshots, backups, archives) that
# would otherwise spam the alert every day.
IGNORE_DIR_NAMES = {
    "deploy-backups",
    "workspace-snapshots",
    "archive",
    ".dropbox.cache",
    ".git",
    "openclaw-installer",
}
# File suffixes that are expected to be large by design (tarballs,
# zip archives) — same rationale.
IGNORE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")


def _is_ignored(path: Path, root: Path) -> bool:
    name = path.name.lower()
    for suffix in IGNORE_SUFFIXES:
        if name.endswith(suffix):
            return True
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    for part in rel_parts:
        if part in IGNORE_DIR_NAMES:
            return True
    return False


def find_large_files(root: Path, threshold: int = SIZE_THRESHOLD_BYTES) -> list[tuple[str, int]]:
    """Return list of (relative_path, size_bytes) for files larger than
    threshold. Skips deploy-backups, workspace-snapshots, archive, and
    archive-format suffixes — see IGNORE_DIR_NAMES / IGNORE_SUFFIXES."""
    if not root.exists():
        return []
    found: list[tuple[str, int]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_ignored(path, root):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > threshold:
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            found.append((rel, size))
    found.sort(key=lambda t: -t[1])
    return found


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / 1024:.0f} KB"


def format_alert(large_files: list[tuple[str, int]]) -> str:
    lines: list[str] = [
        f"\U0001f9ea\U0001f527 Large files in brain: {len(large_files)}",
        "",
    ]
    for path, size in large_files[:20]:
        lines.append(f"  \u2022 {_human_size(size)}  {path}")
    if len(large_files) > 20:
        lines.append(f"  \u2026 {len(large_files) - 20} more")
    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now = datetime.now(timezone.utc)
    large = find_large_files(SCAN_ROOT)

    sent_count = 0
    if large:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        msg = format_alert(large)
        if send_message(token, chat_id, msg, silent=False):
            sent_count = 1

    result = {
        "status": "ok",
        "large_files": len(large),
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
            "alert": f"\U0001f9ea\U0001f527 file-size-monitor failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
