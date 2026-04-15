#!/usr/bin/env python3
"""cron-self-check.py — Mr Fixit fleet host-cron presence check.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`fix-it:cron-self-check`. The old version walked `openclaw cron list`
and matched against `expected-crons.json`. After Phase 4 the OpenClaw
registry is empty and that check has nothing to compare against.

The replacement parses install-host-cron.sh's CONTRACT_ENTRIES +
DIRECT_ENTRIES bash arrays and diffs the marker comments against the
live `crontab -l` output. If any expected marker is missing, send a
Telegram alert. The fix is for the operator to re-run install-host-cron.sh —
this cron diagnoses, it does NOT auto-install (manual gate is the
whole point of the rewrite).

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import re
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
LAST_RUN_FILE = CACHE_DIR / "last-cron-self-check.json"
INSTALL_SCRIPT = Path("/home/openclaw/repo/ops/scripts/install-host-cron.sh")

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


# Match a bash array start: `NAME_ENTRIES=(`
_ARRAY_START_RE = re.compile(r"^\s*([A-Z_]+ENTRIES)\s*=\s*\(\s*$")
# Each entry is a quoted string ending with a `# marker-name`-style segment.
_ENTRY_LINE_RE = re.compile(r'^\s*"([^"]+)"\s*$')


def parse_install_script(path: Path) -> list:
    """Read install-host-cron.sh and return a flat list of expected
    entries — one dict per CONTRACT_ENTRIES or DIRECT_ENTRIES item.

    Each dict carries:
      - kind: "contract" | "direct"
      - schedule: the cron schedule string
      - logname: the contract logname (or wrapper name for direct)
      - marker: the trailing `# marker-name` comment
      - raw: the original entry string
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    expected: list = []

    current_array: str | None = None
    for line in text.splitlines():
        m = _ARRAY_START_RE.match(line)
        if m:
            current_array = m.group(1)
            continue
        if current_array is None:
            continue
        if line.strip() == ")":
            current_array = None
            continue
        em = _ENTRY_LINE_RE.match(line)
        if not em:
            continue
        raw = em.group(1)
        parts = raw.split("|")
        if current_array == "DIRECT_ENTRIES":
            # "<schedule>|<wrapper>|<marker>"
            if len(parts) < 3:
                continue
            schedule = parts[0]
            logname = parts[1]
            marker = parts[2].strip()
            expected.append({
                "kind": "direct",
                "schedule": schedule,
                "logname": logname,
                "marker": marker,
                "raw": raw,
            })
        elif current_array == "CONTRACT_ENTRIES":
            # "<schedule>|<logname>|<container-script>|<env>|<timeout>"
            if len(parts) < 5:
                continue
            schedule = parts[0]
            logname = parts[1]
            marker = f"# script-contract-{logname}"
            expected.append({
                "kind": "contract",
                "schedule": schedule,
                "logname": logname,
                "marker": marker,
                "raw": raw,
            })
    return expected


def _read_crontab() -> str:
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def find_missing_markers(expected: list, crontab: str) -> list:
    """Return the subset of expected entries whose marker is NOT
    present (as a literal substring) anywhere in the crontab output."""
    missing: list = []
    for entry in expected:
        marker = entry.get("marker") or ""
        if not marker:
            continue
        if marker not in crontab:
            missing.append(entry)
    return missing


def format_alert(missing: list) -> str:
    """Render a Telegram message listing missing host-cron markers."""
    lines: list[str] = [
        f"\U0001f9ea\U0001f527 Host-cron drift: {len(missing)} missing",
        "",
    ]
    for entry in missing:
        logname = entry.get("logname", "?")
        schedule = entry.get("schedule", "?")
        lines.append(f"  \u2022 {logname}  ({schedule})")
    lines.append("")
    lines.append(
        "Fix: ssh to the VPS and run "
        "`~/repo/ops/scripts/install-host-cron.sh` to re-install."
    )
    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)

    if not INSTALL_SCRIPT.exists():
        result = {
            "status": "degraded",
            "alert": f"install-host-cron.sh not found at {INSTALL_SCRIPT}",
            "expected": 0,
            "missing": 0,
            "sent": 0,
        }
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps({"timestamp": now_utc.isoformat(), **result}, indent=2),
        )
        return result

    expected = parse_install_script(INSTALL_SCRIPT)
    crontab = _read_crontab()
    missing = find_missing_markers(expected, crontab)

    sent_count = 0
    if missing:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        msg = format_alert(missing)
        if send_message(token, chat_id, msg, silent=False):
            sent_count = 1

    result = {
        "status": "ok",
        "expected": len(expected),
        "missing": len(missing),
        "sent": sent_count,
    }
    _write_atomic(
        LAST_RUN_FILE,
        json.dumps({"timestamp": now_utc.isoformat(), **result}, indent=2),
    )
    return result


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f9ea\U0001f527 cron-self-check failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
