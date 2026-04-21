"""state_introspection — let an agent answer "did you run today?"
factually instead of confabulating from its static role prompt.

The fleet writes a uniform host-cron log at ~/.clawford/logs/<logname>-host.log
via `ops/scripts/script-contract-host.sh`. Each run produces a header
line and a JSON envelope:

    [YYYY-MM-DDTHH:MM:SSZ] <logname> exit=<code>
    {"status": "ok|degraded|error", ...}

`recent_runs(agent_id, since_hours=24)` scans every `<agent_id>-*-host.log`,
filters to the PT-relative window, and returns a small summary the LLM
can cite directly. Shared helper; each agent's `tools.py` wraps it as a
`get_recent_runs` tool (the wire-in lives in the per-agent file — this
module stays framework-agnostic).

Failure posture: missing logs dir, missing files, malformed JSON lines,
or unparseable timestamps all degrade gracefully to an empty or
partial result. Never raises — callers feed the return straight to an
LLM and a hard error would blow up the whole turn.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LOGS_DIR_ENV = "CLAWFORD_LOGS_DIR"
DEFAULT_LOGS_DIR = "~/.clawford/logs"

PT = ZoneInfo("America/Los_Angeles")

# Lines with this shape start a new run entry. Lines that share the
# `[<ts>] <logname> ...` prefix but aren't `exit=` (e.g. `alert sent`,
# `skipped (lock held)`) are NOT new runs — they're footer noise.
_HEADER_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\] "
    r"(?P<name>\S+) exit=(?P<code>\d+)\s*$"
)

# Subset of envelope fields worth surfacing to the LLM. Keep the list
# short — the goal is "at a glance" grounding, not a raw dump.
_SUMMARY_KEYS = (
    "processed",
    "skipped_logged",
    "skipped_service",
    "skipped_unknown_sender",
    "skipped_brian_last",
    "queued",
    "threads_scanned",
    "window_days",
    "updated",
    "people_updated",
    "alert",
    "degraded_reason",
)


def _logs_dir() -> Path:
    raw = os.environ.get(LOGS_DIR_ENV) or DEFAULT_LOGS_DIR
    return Path(os.path.expanduser(raw))


def _parse_utc_ts(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _envelope_summary(env: dict) -> dict:
    return {k: env[k] for k in _SUMMARY_KEYS if k in env}


def _parse_log(path: Path, cutoff_utc: datetime) -> list[dict]:
    """Return finalized run dicts from one log file, newest first."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    runs: list[dict] = []
    current: dict | None = None

    def _finalize(cur: dict | None) -> None:
        if cur is None:
            return
        # Resolve envelope: the LAST line before the next header that
        # parses as JSON wins. Otherwise status=unknown.
        envelope: dict | None = None
        for line in cur["_lines"]:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                envelope = parsed
        status = "unknown"
        summary: dict = {}
        if envelope is not None:
            raw_status = str(envelope.get("status", "")).lower()
            if raw_status in ("ok", "degraded", "error"):
                status = raw_status
            summary = _envelope_summary(envelope)
        cur_out = {
            "name": cur["name"],
            "at_utc": cur["at_utc"].isoformat(),
            "at_pt": cur["at_utc"].astimezone(PT).isoformat(),
            "exit_code": cur["exit_code"],
            "status": status,
            "summary": summary,
        }
        del cur["_lines"]
        runs.append(cur_out)

    for raw_line in text.splitlines():
        m = _HEADER_RE.match(raw_line)
        if m:
            _finalize(current)
            ts = _parse_utc_ts(m.group("ts"))
            if ts is None or ts < cutoff_utc:
                current = None  # skip out-of-window runs
                continue
            current = {
                "name": m.group("name"),
                "at_utc": ts,
                "exit_code": int(m.group("code")),
                "_lines": [],
            }
            continue
        if current is None:
            continue
        # Another bracketed non-exit line — treat as end-of-current-run
        # noise, don't append.
        if raw_line.startswith("["):
            continue
        current["_lines"].append(raw_line)

    _finalize(current)
    return runs


def recent_runs(agent_id: str, since_hours: int = 24) -> dict:
    """Summarize an agent's recent cron-run activity.

    Scans ~/.clawford/logs/<agent_id>-*-host.log (override with
    CLAWFORD_LOGS_DIR for tests), filters to the last `since_hours`
    relative to now (UTC cutoff, reported in PT for the LLM), and
    returns per-run + aggregate counts.

    Return shape:
        {
          "agent_id": "connector",
          "since_hours": 24,
          "now_pt": "...",
          "runs": [ {name, at_pt, status, summary, ...}, ... ],
          "counts": {"ok": N, "degraded": N, "error": N, "unknown": N},
          "by_name": {"<logname>": count, ...},
        }
    """
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff_utc = now_utc - timedelta(hours=since_hours)
    now_pt = now_utc.astimezone(PT)

    empty_counts = {"ok": 0, "degraded": 0, "error": 0, "unknown": 0}
    result: dict[str, Any] = {
        "agent_id": agent_id,
        "since_hours": since_hours,
        "now_pt": now_pt.isoformat(),
        "runs": [],
        "counts": dict(empty_counts),
        "by_name": {},
    }

    logs_dir = _logs_dir()
    if not logs_dir.is_dir():
        return result

    prefix = f"{agent_id}-"
    pattern = "-host.log"
    for path in sorted(logs_dir.iterdir()):
        name = path.name
        if not name.startswith(prefix) or not name.endswith(pattern):
            continue
        result["runs"].extend(_parse_log(path, cutoff_utc))

    result["runs"].sort(key=lambda r: r["at_utc"], reverse=True)

    for r in result["runs"]:
        status = r["status"]
        result["counts"][status] = result["counts"].get(status, 0) + 1
        n = r["name"]
        result["by_name"][n] = result["by_name"].get(n, 0) + 1

    return result
