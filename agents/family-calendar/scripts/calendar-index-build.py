#!/usr/bin/env python3
"""calendar-index-build.py — build the shared brain calendar index.

Runs once per morning tick (10:25 UTC) ahead of the 10:30 UTC agent
morning-briefings. Fetches the next 8 days of events from every
calendar in family-calendar/calendar-config.json using Mouse's token
(the broadest-access token on the VPS), classifies each raw event
with agents.shared.calendar_index.classify_event, and writes the
result to ~/Dropbox/openclaw-backup/status/calendar-index.json.

Both Sergeant Murphy and Mistress Mouse read the index to decide who
owns each event. Descriptions are consumed by the classifier but NOT
written to the brain file — only the derived flags (has_video_link,
is_meeting, owner) land in the index.

If this script fails, downstream agents fall back to their local
has_videoconference_link check on whatever event data they can see.
That's degraded — Mouse's local path still can't see a description-
embedded link — but the fleet keeps running.

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.calendar_index import classify_event  # noqa: E402

WORKSPACE = Path(os.path.expanduser("~/.clawford/family-calendar-workspace"))
CONFIG_PATH = WORKSPACE / "calendar-config.json"
TOKEN_PATH = Path(os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    str(WORKSPACE / "token.json"),
))
BRAIN_STATUS_DIR = Path(os.environ.get(
    "CLAWFORD_BRAIN_STATUS_DIR",
    os.path.expanduser("~/Dropbox/openclaw-backup/status"),
))
INDEX_PATH = BRAIN_STATUS_DIR / "calendar-index.json"
WORKFLOWY_LINKS_PATH = Path(os.environ.get(
    "WORKFLOWY_LINKS_PATH",
    os.path.expanduser(
        "~/.clawford/meetings-coach-workspace/cache/workflowy-links.json"
    ),
))
LOOKAHEAD_DAYS = 8


def _load_workflowy_event_ids() -> set:
    """Return the set of event IDs the operator has linked in Workflowy.
    Missing/malformed file → empty set so the routing degrades to the
    pure videoconference rule."""
    if not WORKFLOWY_LINKS_PATH.exists():
        return set()
    try:
        data = json.loads(WORKFLOWY_LINKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    return set(data.keys())


def _get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        return None, f"token.json not found at {TOKEN_PATH}"
    token_data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_data["token"] = creds.token
            TOKEN_PATH.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        except Exception as e:
            return None, f"token refresh failed: {e}"
    if not creds.valid:
        return None, "credentials invalid — run gcal-auth.py"
    return creds, None


def _fetch_events(service, calendar_id: str, time_min: str, time_max: str) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while True:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        ).execute()
        items.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items


def build_index(
    config: dict,
    service,
    now_utc: datetime,
    *,
    workflowy_event_ids: set | None = None,
) -> dict:
    time_min = now_utc.isoformat()
    time_max = (now_utc + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    wf_ids = workflowy_event_ids or set()

    events: dict[str, dict] = {}
    errors: list[dict] = []
    scanned = 0

    for cal in config.get("calendars", []):
        cal_id = cal.get("id")
        if not cal_id:
            continue
        try:
            raw_events = _fetch_events(service, cal_id, time_min, time_max)
        except Exception as e:
            errors.append({"calendar_id": cal_id, "error": str(e)})
            continue
        for raw in raw_events:
            if raw.get("status") == "cancelled":
                continue
            eid = raw.get("id")
            if not eid:
                continue
            scanned += 1
            rec = classify_event(raw, calendar_id=cal_id, workflowy_event_ids=wf_ids)
            # If the same event appears on multiple calendars (invite
            # propagation), prefer the classification that sees a video
            # link — one authoritative copy wins over a stripped copy.
            existing = events.get(eid)
            if existing is None or (not existing.get("is_meeting") and rec.get("is_meeting")):
                events[eid] = rec

    return {
        "generated_at": now_utc.isoformat(),
        "window": {
            "start": time_min[:10],
            "end": time_max[:10],
        },
        "lookahead_days": LOOKAHEAD_DAYS,
        "events": events,
        "event_count": len(events),
        "scanned_count": scanned,
        "workflowy_link_count": len(wf_ids),
        "errors": errors,
    }


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def run() -> dict:
    if not CONFIG_PATH.exists():
        return {"status": "error", "error": f"missing {CONFIG_PATH}"}
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    creds, err = _get_credentials()
    if err:
        return {"status": "error", "error": err}

    try:
        from googleapiclient.discovery import build as gbuild
        service = gbuild("calendar", "v3", credentials=creds)
    except Exception as e:
        return {"status": "error", "error": f"failed to build Calendar service: {e}"}

    workflowy_ids = _load_workflowy_event_ids()
    now_utc = datetime.now(timezone.utc)
    index = build_index(config, service, now_utc, workflowy_event_ids=workflowy_ids)
    _write_atomic(INDEX_PATH, index)

    degraded = bool(index.get("errors"))
    result = {
        "status": "degraded" if degraded else "ok",
        "event_count": index["event_count"],
        "meeting_count": sum(1 for r in index["events"].values() if r.get("is_meeting")),
        "lookahead_days": LOOKAHEAD_DAYS,
        "index_path": str(INDEX_PATH),
        "errors": index["errors"],
    }
    if degraded:
        result["alert"] = (
            f"calendar-index-build: {len(index['errors'])} calendar(s) failed"
        )
    return result


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc().splitlines()[-3:],
            "alert": f"calendar-index-build crashed: {e}",
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
