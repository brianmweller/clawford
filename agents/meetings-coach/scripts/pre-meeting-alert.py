#!/usr/bin/env python3
"""pre-meeting-alert.py — Meetings Coach (Sergeant Murphy) pre-meeting alerter.

Phase 4 orchestrator that replaces the OpenClaw LLM cron
`meetings-coach:pre-meeting-alert`. Every 30 min, runs gcal-fetch.py,
filters real meetings starting in 15-45 minutes, dedupes against
sent-alerts.json, and sends one Telegram alert per new meeting with
existing open commitments and Workflowy agenda items (no invented
talking points).

Pure Python — meeting-prep.py output is structured. SCRIPT_CONTRACT-
compliant: always exits 0, prints one JSON line.
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
SENT_ALERTS_FILE = WORKSPACE / "sent-alerts.json"
LAST_RUN_FILE = CACHE_DIR / "last-pre-meeting.json"

BOT_TOKEN_ENV = "MEETINGS_BOT_TOKEN"
SUBPROCESS_TIMEOUT_S = 90

ALERT_WINDOW_MIN_MINUTES = 15
ALERT_WINDOW_MAX_MINUTES = 45


def _run_script(script_name: str, *args: str, timeout: int = SUBPROCESS_TIMEOUT_S):
    """Shim over agents.shared.subprocess_helpers.run_json_script so existing
    call sites keep working. Returns parsed JSON on success or
    {'__error__': ...} on any subprocess-level failure."""
    return run_json_script(str(SCRIPTS_DIR / script_name), *args, timeout=timeout)


def _parse_event_dt(iso_str: str) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def filter_upcoming(events: list, now_utc: datetime) -> list:
    """Return events that are real meetings starting within
    [ALERT_WINDOW_MIN_MINUTES, ALERT_WINDOW_MAX_MINUTES) from now."""
    upcoming: list = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if not event.get("is_real_meeting", False):
            continue
        start = _parse_event_dt(event.get("start", ""))
        if start is None:
            continue
        delta_min = (start - now_utc).total_seconds() / 60.0
        if ALERT_WINDOW_MIN_MINUTES <= delta_min < ALERT_WINDOW_MAX_MINUTES:
            upcoming.append(event)
    upcoming.sort(key=lambda e: e.get("start", ""))
    return upcoming


def _meeting_result(prep: dict) -> dict | None:
    if not isinstance(prep, dict):
        return None
    meetings = prep.get("meetings") or []
    if not meetings:
        return None
    m = meetings[0]
    return m if isinstance(m, dict) else None


def _open_commitments_from_prep(prep: dict) -> list:
    m = _meeting_result(prep)
    if m is None:
        return []
    ctx = m.get("context") or {}
    commitments = ctx.get("commitments") or []
    return [c for c in commitments if c.get("status") in ("open", "overdue")]


def _professional_compact_lines(prep: dict) -> list[str]:
    """Compact professional-meeting prep block for the 15-min alert:
    one header line + one question + one talking point. Returns [] when
    the meeting isn't professional or llm_prep has an error."""
    m = _meeting_result(prep)
    if m is None:
        return []
    meeting_type = m.get("meeting_type", "general")
    if meeting_type == "general":
        return []

    lines: list[str] = []
    target = (m.get("self_context") or {}).get("target_company")
    stage = (m.get("self_context") or {}).get("active_pipeline_stage")
    header = f"\U0001f4bc {meeting_type}"
    if isinstance(target, dict) and target.get("company"):
        header += f" · target {target['company']}"
    if isinstance(stage, dict) and stage.get("stage"):
        header += f" · pipeline {stage['stage']}"
    lines.append(header)

    llm_prep = m.get("llm_prep") or {}
    if "error" in llm_prep:
        return lines

    compelling = (llm_prep.get("compelling_angle") or "").strip()
    if compelling:
        lines.append(f"✨ Why: {compelling}")

    questions = (llm_prep.get("evaluation_questions")
                 or llm_prep.get("questions_to_ask") or [])
    if questions:
        lines.append(f"\U0001f4ac Ask: {questions[0]}")
    evidence = (llm_prep.get("fit_evidence")
                or llm_prep.get("talking_points") or [])
    if evidence:
        lines.append(f"\U0001f4e2 Drop-in: {evidence[0]}")
    return lines


def format_alert(event: dict, prep: dict, agenda: list, now_utc: datetime) -> str:
    """Render one pre-meeting alert message."""
    title = event.get("summary") or "(untitled)"
    start = _parse_event_dt(event.get("start", ""))
    minutes_until = int(round((start - now_utc).total_seconds() / 60.0)) if start else 0

    attendees = event.get("attendees") or []
    names = [
        (a.get("name") or a.get("email") or "?").strip()
        for a in attendees
        if isinstance(a, dict)
    ]
    names_str = ", ".join(n for n in names if n)

    lines: list[str] = [
        f"\U0001f437\U0001f50d Heads up — meeting in {minutes_until} min",
        f"\U0001f4cb {title}",
    ]
    if names_str:
        lines.append(f"\U0001f465 {names_str}")

    # Professional-meeting compact prep block — only when classifier hit.
    lines.extend(_professional_compact_lines(prep))

    open_commits = _open_commitments_from_prep(prep)
    if open_commits:
        for c in open_commits:
            who = (c.get("who") or "?").strip()
            to_whom = (c.get("to_whom") or "?").strip()
            what = (c.get("what") or "").strip()
            lines.append(f"\U0001f4cc Open: {who} \u2192 {to_whom}: {what}")

    if agenda:
        lines.append("\U0001f4d4 Agenda:")
        for item in agenda:
            text = item.strip() if isinstance(item, str) else str(item)
            lines.append(f"  \u2022 {text}")

    return "\n".join(lines).rstrip() + "\n"


def _load_sent_ids() -> set:
    if not SENT_ALERTS_FILE.exists():
        return set()
    try:
        with SENT_ALERTS_FILE.open(encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(entries, list):
        return set()
    return {str(e.get("id")) for e in entries if isinstance(e, dict) and e.get("id")}


def _append_sent_ids(ids: list[str]) -> None:
    existing: list = []
    if SENT_ALERTS_FILE.exists():
        try:
            with SENT_ALERTS_FILE.open(encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = []
    if not isinstance(existing, list):
        existing = []

    stamp = datetime.now(timezone.utc).isoformat()
    known = {str(e.get("id")) for e in existing if isinstance(e, dict) and e.get("id")}
    for nid in ids:
        if nid in known:
            continue
        existing.append({"id": nid, "alerted_at": stamp})

    SENT_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SENT_ALERTS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    tmp.replace(SENT_ALERTS_FILE)


def _read_workflowy_agenda(event_id: str) -> list:
    """Best-effort read of Workflowy agenda items for an event. Tolerates
    failures — agenda is optional in the alert."""
    out = _run_script("workflowy-sync.py", "--read-agenda", event_id, timeout=60)
    if is_subprocess_error(out) or not isinstance(out, dict):
        return []
    items = out.get("agenda") or out.get("items") or []
    if not isinstance(items, list):
        return []
    cleaned: list = []
    for item in items:
        if isinstance(item, str):
            cleaned.append(item)
        elif isinstance(item, dict):
            txt = (item.get("text") or item.get("name") or "").strip()
            if txt:
                cleaned.append(txt)
    return cleaned


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def run() -> dict:
    now_utc = datetime.now(timezone.utc)

    gcal = _run_script("gcal-fetch.py")

    # gcal-fetch is the primary data source — without it we can't know
    # which meetings are upcoming, so propagate subprocess failures as
    # top-level errors instead of "no new meetings".
    if is_subprocess_error(gcal):
        error_msg = gcal["__error__"]
        _write_atomic(
            LAST_RUN_FILE,
            json.dumps(
                {
                    "timestamp": now_utc.isoformat(),
                    "status": "error",
                    "error": error_msg,
                    "summary": f"gcal-fetch failed: {error_msg[:120]}",
                },
                indent=2,
            ),
        )
        return {
            "status": "error",
            "error": error_msg,
            "alert": f"\U0001f437\U0001f50d pre-meeting-alert failed: {error_msg[:200]}",
        }

    events = gcal.get("events", []) if isinstance(gcal, dict) else []

    upcoming = filter_upcoming(events, now_utc)
    sent_ids = _load_sent_ids()
    new_meetings = [e for e in upcoming if e.get("id") and e["id"] not in sent_ids]

    sent_count = 0
    sent_new_ids: list[str] = []

    if new_meetings:
        token, chat_id = resolve_credentials(BOT_TOKEN_ENV)
        for event in new_meetings:
            event_id = event["id"]
            # meeting-prep is optional — render the alert with whatever
            # we got. A prep failure must not block the alert itself.
            prep = _run_script("meeting-prep.py", "--meeting-id", event_id)
            if is_subprocess_error(prep) or not isinstance(prep, dict):
                prep = {}
            agenda = _read_workflowy_agenda(event_id)
            msg = format_alert(event, prep, agenda, now_utc)
            if send_message(token, chat_id, msg, silent=False):
                sent_count += 1
                sent_new_ids.append(event_id)

    if sent_new_ids:
        _append_sent_ids(sent_new_ids)

    _write_atomic(
        LAST_RUN_FILE,
        json.dumps(
            {
                "timestamp": now_utc.isoformat(),
                "status": "ok",
                "upcoming": len(upcoming),
                "new_meetings": len(new_meetings),
                "sent": sent_count,
            },
            indent=2,
        ),
    )

    return {
        "status": "ok",
        "upcoming": len(upcoming),
        "new_meetings": len(new_meetings),
        "sent": sent_count,
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"\U0001f437\U0001f50d pre-meeting-alert failed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
