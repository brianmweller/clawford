"""_cron_lookup — discover host and LLM crons by name.

Backs propose_rerun_cron in tools.py. Mirrors the parsing in
agents/fix-it/scripts/cron-self-check.py without importing it (the
script's filename has a hyphen, so it cannot be imported as a regular
module without importlib.util gymnastics that don't belong inside a
production tool path).

Three things to know about names:
  - DIRECT host cron name = wrapper basename without `.sh`
    (e.g. "morning-fleet-deliver-host", "costco-token-refresh-host")
  - CONTRACT host cron name = the logname parts[1] from the entry
    (e.g. "fix-it-conflict-scan", "shopping-delivery-digest")
  - LLM cron name = the inner key in expected-crons.json
    (e.g. "weekly-review", "morning-meeting-brief")

Returns from list_host_crons() and list_llm_crons() carry enough
context for confirm_rerun_cron to invoke the right wrapper without
re-parsing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "ops" / "scripts" / "install-host-cron.sh"
WRAPPER_DIR = REPO_ROOT / "ops" / "scripts"
AGENTS_DIR = REPO_ROOT / "agents"

_ARRAY_START_RE = re.compile(r"^\s*([A-Z_]+ENTRIES)\s*=\s*\(\s*$")
_ENTRY_LINE_RE = re.compile(r'^\s*"([^"]+)"\s*$')


def _parse_install_script(path: Path) -> list[dict]:
    """Parse install-host-cron.sh's DIRECT_ENTRIES + CONTRACT_ENTRIES
    bash arrays into one flat list of host-cron entries."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    entries: list[dict] = []
    current_array: Optional[str] = None
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
        if current_array == "DIRECT_ENTRIES" and len(parts) >= 3:
            schedule, wrapper_basename, _marker = parts[0], parts[1], parts[2]
            name = wrapper_basename
            if name.endswith(".sh"):
                name = name[:-3]
            entries.append({
                "name": name,
                "kind": "host",
                "contract": False,
                "schedule": schedule,
                "wrapper_path": WRAPPER_DIR / wrapper_basename,
            })
        elif current_array == "CONTRACT_ENTRIES" and len(parts) >= 5:
            schedule, logname, script_path, token_env, timeout_s = parts[:5]
            entries.append({
                "name": logname,
                "kind": "host",
                "contract": True,
                "schedule": schedule,
                "wrapper_path": WRAPPER_DIR / "script-contract-host.sh",
                "script_path": script_path,
                "token_env": token_env,
                "timeout_s": timeout_s,
            })
    return entries


def list_host_crons() -> list[dict]:
    """Return all host-cron entries (DIRECT + CONTRACT) from install-host-cron.sh."""
    return _parse_install_script(INSTALL_SCRIPT)


def list_llm_crons() -> list[dict]:
    """Walk agents/*/expected-crons.json and return LLM-cron entries.

    LLM crons are post-Phase-7 retired (no runner exists), but they
    remain declared in expected-crons.json so the lookup surfaces
    them — confirm_rerun_cron then returns a clear error pointing
    operators at install-host-cron.sh as the conversion target.
    """
    out: list[dict] = []
    if not AGENTS_DIR.exists():
        return out
    for path in sorted(AGENTS_DIR.glob("*/expected-crons.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for agent, crons in data.items():
            if not isinstance(crons, dict):
                continue
            for cron_name, cron_def in crons.items():
                if not isinstance(cron_def, dict):
                    continue
                out.append({
                    "name": cron_name,
                    "kind": "llm",
                    "agent": agent,
                    "schedule": cron_def.get("cron", ""),
                })
    return out


def lookup_cron(name: str) -> Optional[dict]:
    """Return the first matching cron entry, host preferred over LLM."""
    for entry in list_host_crons():
        if entry["name"] == name:
            return entry
    for entry in list_llm_crons():
        if entry["name"] == name:
            return entry
    return None
