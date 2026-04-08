#!/usr/bin/env python3
"""
gcal-fetch.py — Fetch events from Sam's professional Google Calendar.

Reads meeting-config.json for the calendar ID and filters. Fetches events
via Google Calendar API. Enriches with attendee details, conference links,
and a real-meeting filter. Outputs JSON to stdout.

Usage:
  python3 gcal-fetch.py                     # Today's events
  python3 gcal-fetch.py --date 2026-04-08   # Specific date
  python3 gcal-fetch.py --days 7            # Next N days

Output JSON:
  {
    "status": "ok",
    "date": "2026-04-08",
    "days": 1,
    "events": [...],
    "errors": [],
    "fetched_at": "..."
  }

Requires: google-api-python-client, google-auth-httplib2, google-auth-oauthlib
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

WORKSPACE = os.path.expanduser("~/.openclaw/meetings-coach-workspace")
CONFIG_PATH = os.path.join(WORKSPACE, "meeting-config.json")
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

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--date" and i + 1 < len(sys.argv):
            target_date = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
            i += 2
        else:
            i += 1

    return target_date, days


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return None, "meeting-config.json not found"
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


def extract_conference_link(event):
    """Extract video meeting link from event data."""
    # Check hangoutLink first (Google Meet)
    link = event.get("hangoutLink", "")
    if link:
        return link

    # Check conferenceData for other providers (Zoom, Teams, etc.)
    conf = event.get("conferenceData", {})
    for entry_point in conf.get("entryPoints", []):
        if entry_point.get("entryPointType") == "video":
            return entry_point.get("uri", "")

    # Check description for common meeting URLs
    desc = event.get("description", "")
    if desc:
        for pattern in ["https://zoom.us/", "https://meet.google.com/", "https://teams.microsoft.com/"]:
            idx = desc.find(pattern)
            if idx >= 0:
                # Extract URL (up to whitespace or newline)
                end = len(desc)
                for ch in [" ", "\n", "\r", '"', "'"]:
                    pos = desc.find(ch, idx)
                    if pos >= 0:
                        end = min(end, pos)
                return desc[idx:end]

    return ""


def is_real_meeting(event, config):
    """Determine if an event is a real meeting vs. a task block or reminder."""
    filters = config.get("meeting_filters", {})
    summary = event.get("summary", "")

    # Check skip titles
    skip_titles = filters.get("skip_titles", [])
    for skip in skip_titles:
        if skip.lower() in summary.lower():
            return False

    if not filters.get("require_attendees_or_video", True):
        return True

    # Real meeting = has attendees (besides organizer) or has a video link
    attendees = event.get("attendees", [])
    non_self_attendees = [a for a in attendees if not a.get("self", False)]
    has_attendees = len(non_self_attendees) > 0
    has_video = bool(extract_conference_link(event))

    return has_attendees or has_video


def fetch_calendar_events(service, calendar_id, time_min, time_max, config):
    """Fetch events from the calendar with full attendee and conference data."""
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

            all_day = "date" in start
            start_str = start.get("dateTime", start.get("date", ""))
            end_str = end.get("dateTime", end.get("date", ""))

            # Extract attendees
            raw_attendees = event.get("attendees", [])
            attendees = []
            for att in raw_attendees:
                if att.get("self", False):
                    continue
                attendees.append({
                    "email": att.get("email", ""),
                    "name": att.get("displayName", att.get("email", "").split("@")[0]),
                    "response_status": att.get("responseStatus", "needsAction"),
                })

            conference_link = extract_conference_link(event)
            real = is_real_meeting(event, config)

            events.append({
                "id": event.get("id", ""),
                "summary": event.get("summary", "(No title)"),
                "start": start_str,
                "end": end_str,
                "all_day": all_day,
                "location": event.get("location", ""),
                "description": event.get("description", ""),
                "status": event.get("status", "confirmed"),
                "attendees": attendees,
                "conference_link": conference_link,
                "is_real_meeting": real,
                "organizer": event.get("organizer", {}).get("email", ""),
                "source_calendar_id": calendar_id,
            })

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return events


def main():
    target_date, days = parse_args()

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
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(tz_name))
            base = datetime(now.year, now.month, now.day)
        except ImportError:
            base = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    # Google Calendar API wants RFC3339 with timezone
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        time_min = base.replace(tzinfo=tz).isoformat()
        time_max = (base + timedelta(days=days)).replace(tzinfo=tz).isoformat()
    except ImportError:
        time_min = base.isoformat() + "T00:00:00Z"
        time_max = (base + timedelta(days=days)).isoformat() + "T00:00:00Z"

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

    # Fetch events
    all_events = []
    errors = []
    calendars = config.get("calendars", [])

    for cal in calendars:
        cal_id = cal["id"]
        label = cal["label"]
        emoji = cal.get("emoji", "")

        try:
            events = fetch_calendar_events(service, cal_id, time_min, time_max, config)
            for event in events:
                event["calendar_label"] = label
                event["calendar_emoji"] = emoji
            all_events.extend(events)
        except Exception as e:
            errors.append({"calendar": label, "calendar_id": cal_id, "error": str(e)})

    # Sort by start time
    all_events.sort(key=lambda e: e["start"])

    # Cache results
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_date = target_date or base.strftime("%Y-%m-%d")
    cache_path = os.path.join(CACHE_DIR, f"events-{cache_date}.json")

    result = {
        "status": "ok" if not errors else "partial",
        "date": cache_date,
        "days": days,
        "events": all_events,
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(cache_path, "w") as f:
        json.dump(result, f, indent=2)

    # Output to stdout
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
