#!/usr/bin/env python3
"""
gcal-fetch.py — Fetch events from multiple Google Calendars.

Reads calendar-config.json for the list of calendars and family member
mapping. Fetches events via Google Calendar API. Detects scheduling
conflicts across calendars. Outputs JSON to stdout.

Usage:
  python3 gcal-fetch.py                     # Today's events
  python3 gcal-fetch.py --date 2026-04-08   # Specific date
  python3 gcal-fetch.py --days 7            # Next N days
  python3 gcal-fetch.py --calendar-id X     # Single calendar only

Output JSON:
  {
    "status": "ok",
    "date": "2026-04-08",
    "days": 1,
    "events": [...],
    "conflicts": [...],
    "errors": [...]
  }

Requires: google-api-python-client, google-auth-httplib2, google-auth-oauthlib
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.meeting_classifier import has_videoconference_link  # noqa: E402
from agents.shared.calendar_index import meeting_event_ids  # noqa: E402

WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")

BRAIN_INDEX_PATH = os.environ.get(
    "CLAWFORD_CALENDAR_INDEX_PATH",
    os.path.expanduser("~/Dropbox/openclaw-backup/status/calendar-index.json"),
)


def _load_brain_meeting_ids() -> set:
    """Read the shared brain calendar index's meeting ids. Degrades to
    empty set on any error so the local has_meeting_link fallback owns
    classification when the index is missing/stale."""
    try:
        return meeting_event_ids(BRAIN_INDEX_PATH)
    except Exception:
        return set()
CONFIG_PATH = os.path.join(WORKSPACE, "calendar-config.json")
TOKEN_PATH = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(WORKSPACE, "token.json"),
)
CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_CALENDAR_CREDENTIALS_PATH",
    os.path.join(WORKSPACE, "credentials.json"),
)
CACHE_DIR = os.path.join(WORKSPACE, "cache")


def parse_args():
    target_date = None
    days = 1
    calendar_id = None
    skip_meetings = False

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--date" and i + 1 < len(sys.argv):
            target_date = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == "--calendar-id" and i + 1 < len(sys.argv):
            calendar_id = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--skip-meetings":
            # Mouse/Murphy boundary: drop events with a videoconference
            # link (those are Murphy's). See memory
            # project_meeting_event_routing.md — rule changed 2026-04-18
            # from "has Workflowy link" to "has videoconference link".
            skip_meetings = True
            i += 1
        else:
            i += 1

    return target_date, days, calendar_id, skip_meetings


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return None, "calendar-config.json not found"
    with open(CONFIG_PATH) as f:
        return json.load(f), None


def get_credentials():
    """Load or refresh OAuth2 credentials."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None, "google-auth not installed"

    if not os.path.exists(TOKEN_PATH):
        return None, f"token.json not found at {TOKEN_PATH} — run gcal-auth.py first"

    with open(TOKEN_PATH) as f:
        token_data = json.load(f)

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
            # Save refreshed token
            token_data["token"] = creds.token
            with open(TOKEN_PATH, "w") as f:
                json.dump(token_data, f, indent=2)
        except Exception as e:
            return None, f"Token refresh failed: {e}"

    if not creds.valid:
        return None, "Credentials invalid — run gcal-auth.py to re-authorize"

    return creds, None


def fetch_calendar_events(service, calendar_id, time_min, time_max):
    """Fetch events from a single calendar."""
    events = []
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

        for event in result.get("items", []):
            start = event.get("start", {})
            end = event.get("end", {})

            # Determine if all-day event
            all_day = "date" in start
            start_str = start.get("dateTime", start.get("date", ""))
            end_str = end.get("dateTime", end.get("date", ""))

            # Classify on the RAW event (description intact). Mouse
            # deliberately drops `description` from its output for
            # display-safety, but the routing predicate needs to see
            # it — a Webex/Zoom/Meet link pasted into the description
            # is the signal that Murphy owns the event. Run the check
            # here, then drop the description from the emitted record.
            has_meeting_link = has_videoconference_link(event)

            events.append({
                "id": event.get("id", ""),
                "summary": event.get("summary", "(No title)"),
                "start": start_str,
                "end": end_str,
                "all_day": all_day,
                "location": event.get("location", ""),
                "description": "",  # Don't include — untrusted data, not needed for display
                "status": event.get("status", "confirmed"),
                "source_calendar_id": calendar_id,
                "has_meeting_link": has_meeting_link,
            })

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return events


def detect_conflicts(all_events):
    """Find overlapping non-all-day events across different calendars."""
    conflicts = []
    timed_events = [e for e in all_events if not e["all_day"] and e["status"] == "confirmed"]

    for i, a in enumerate(timed_events):
        for b in timed_events[i + 1:]:
            # Skip events from the same calendar (user's own overlap)
            if a["source_calendar_id"] == b["source_calendar_id"]:
                continue

            # Check time overlap
            a_start = a["start"]
            a_end = a["end"]
            b_start = b["start"]
            b_end = b["end"]

            if a_start < b_end and b_start < a_end:
                conflicts.append({
                    "events": [
                        f"{a['calendar_label']}: {a['summary']}",
                        f"{b['calendar_label']}: {b['summary']}",
                    ],
                    "time": a_start,
                    "note": f"Overlap between {a['calendar_label']} and {b['calendar_label']}",
                })

    return conflicts


def dedup_events(events):
    """Remove duplicate events (same title + start time from shared calendars)."""
    seen = {}
    deduped = []

    for event in events:
        key = (event["summary"].strip().lower(), event["start"])
        if key not in seen:
            seen[key] = True
            deduped.append(event)

    return deduped


def main():
    target_date, days, filter_calendar_id, skip_meetings = parse_args()

    # Load config
    config, err = load_config()
    if err:
        print(json.dumps({"status": "error", "message": err}))
        sys.exit(1)

    tz_name = config.get("timezone", "America/Los_Angeles")

    # Compute time range
    if target_date:
        base = datetime.strptime(target_date, "%Y-%m-%d")
    else:
        # Use the configured timezone for "today"
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(tz_name))
            base = datetime(now.year, now.month, now.day)
        except ImportError:
            base = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    time_min = base.isoformat() + "T00:00:00"
    time_max = (base + timedelta(days=days)).isoformat() + "T00:00:00"

    # Add timezone offset for API
    # Google Calendar API wants RFC3339 with timezone
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        time_min = base.replace(tzinfo=tz).isoformat()
        time_max = (base + timedelta(days=days)).replace(tzinfo=tz).isoformat()
    except ImportError:
        # Fallback: assume UTC
        time_min += "Z"
        time_max += "Z"

    # Get credentials
    creds, err = get_credentials()
    if err:
        print(json.dumps({"status": "error", "message": err}))
        sys.exit(1)

    # Build service
    try:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds)
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Failed to build Calendar service: {e}"}))
        sys.exit(1)

    # Fetch events from each calendar
    all_events = []
    errors = []
    calendars = config.get("calendars", [])

    for cal in calendars:
        cal_id = cal["id"]
        label = cal["label"]
        emoji = cal.get("emoji", "")

        if filter_calendar_id and cal_id != filter_calendar_id:
            continue

        try:
            events = fetch_calendar_events(service, cal_id, time_min, time_max)
            for event in events:
                event["calendar_label"] = label
                event["calendar_emoji"] = emoji
            all_events.extend(events)
        except Exception as e:
            errors.append({"calendar": label, "calendar_id": cal_id, "error": str(e)})

    # Sort by start time
    all_events.sort(key=lambda e: e["start"])

    # Dedup shared events
    all_events = dedup_events(all_events)

    # Apply --skip-meetings filter: drop the events that Murphy owns.
    # Authoritative signal is the shared brain calendar index
    # (ops/scripts/calendar-index-build.py), which classifies RAW
    # events once per tick. The local `has_meeting_link` annotation
    # above is a fallback for events the index hasn't seen yet (e.g.
    # a meeting the operator just created mid-day) — it works for events
    # whose video link is in hangoutLink/conferenceData but not for
    # description-only links, because we blank descriptions at fetch
    # time. Those cases rely on the index.
    brain_meeting_ids: set[str] = set()
    if skip_meetings:
        brain_meeting_ids = _load_brain_meeting_ids()

    skipped_count = 0
    if skip_meetings:
        before = len(all_events)
        all_events = [
            e for e in all_events
            if e.get("id", "") not in brain_meeting_ids
            and not e.get("has_meeting_link")
        ]
        skipped_count = before - len(all_events)

    # Detect conflicts
    conflicts = detect_conflicts(all_events)

    # Cache results
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_date = target_date or base.strftime("%Y-%m-%d")
    cache_path = os.path.join(CACHE_DIR, f"events-{cache_date}.json")

    result = {
        "status": "ok" if not errors else "partial",
        "date": cache_date,
        "days": days,
        "events": all_events,
        "conflicts": conflicts,
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "skip_meetings": skip_meetings,
        "skipped_meetings_count": skipped_count,
    }

    with open(cache_path, "w") as f:
        json.dump(result, f, indent=2)

    # Output to stdout
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
