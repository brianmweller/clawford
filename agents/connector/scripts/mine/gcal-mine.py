#!/usr/bin/env python3
"""
gcal-mine.py — Mine 2 years of Google Calendar for attendee contacts.

Fetches all events, extracts unique attendees with meeting counts,
titles, and organizer info.

Usage:
  python3 gcal-mine.py                # Mine last 2 years

Output: cache/mined-gcal.json
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import (
    get_google_credentials,
    is_brian,
    load_config,
    normalize_email,
    save_mined,
)

DEFAULT_TOKEN_PATHS = [
    os.path.expanduser("~/.openclaw/meetings-coach-workspace/token.json"),
    os.path.expanduser("~/.openclaw/family-calendar-workspace/token.json"),
]


def find_token():
    config = load_config()
    custom = config.get("calendar", {}).get("token_path")
    if custom and os.path.exists(custom):
        return custom
    for path in DEFAULT_TOKEN_PATHS:
        if os.path.exists(path):
            return path
    return None


def mine_calendar():
    config = load_config()
    lookback = config.get("lookback_years", 2)
    calendar_ids = config.get("calendar", {}).get("calendar_ids", ["primary"])

    token_path = find_token()
    if not token_path:
        print("ERROR: No Calendar token found.", file=sys.stderr)
        sys.exit(1)

    print(f"Using token: {token_path}", file=sys.stderr)

    creds, err = get_google_credentials(token_path)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=creds)

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=lookback * 365)).isoformat()
    time_max = now.isoformat()

    contacts = defaultdict(lambda: {
        "email": "",
        "display_names": set(),
        "meeting_count": 0,
        "first_meeting": None,
        "last_meeting": None,
        "meeting_titles": [],
        "organizer_count": 0,
        "response_statuses": defaultdict(int),
    })

    total_events = 0

    for cal_id in calendar_ids:
        print(f"Mining calendar: {cal_id}", file=sys.stderr)
        page_token = None

        while True:
            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page_token,
                ).execute()
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
                break

            events = result.get("items", [])

            for event in events:
                total_events += 1
                summary = event.get("summary", "")
                start = event.get("start", {})
                event_date = start.get("dateTime", start.get("date", ""))[:10]

                # Organizer
                organizer_email = normalize_email(
                    event.get("organizer", {}).get("email", "")
                )

                # Attendees
                for att in event.get("attendees", []):
                    if att.get("self", False):
                        continue
                    email = normalize_email(att.get("email", ""))
                    if not email or is_brian(email, config):
                        continue

                    name = att.get("displayName", email.split("@")[0])
                    status = att.get("responseStatus", "needsAction")

                    c = contacts[email]
                    c["email"] = email
                    if name:
                        c["display_names"].add(name)
                    c["meeting_count"] += 1
                    c["response_statuses"][status] += 1

                    if organizer_email == email:
                        c["organizer_count"] += 1

                    if event_date:
                        if not c["first_meeting"] or event_date < c["first_meeting"]:
                            c["first_meeting"] = event_date
                        if not c["last_meeting"] or event_date > c["last_meeting"]:
                            c["last_meeting"] = event_date

                    if summary and len(c["meeting_titles"]) < 30:
                        c["meeting_titles"].append(summary)

            page_token = result.get("nextPageToken")
            if not page_token:
                break
            time.sleep(0.5)

    # Finalize
    output_contacts = {}
    for email, data in contacts.items():
        data["display_names"] = list(data["display_names"])
        data["response_statuses"] = dict(data["response_statuses"])
        # Dedupe meeting titles
        seen = set()
        unique = []
        for t in data["meeting_titles"]:
            key = t.lower().strip()
            if key not in seen:
                seen.add(key)
                unique.append(t)
        data["meeting_titles"] = unique[:20]
        output_contacts[email] = data

    result = {
        "status": "ok",
        "source": "gcal",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "events_scanned": total_events,
        "contacts_found": len(output_contacts),
        "contacts": output_contacts,
    }

    save_mined("gcal", result)
    print(f"\nDone. Scanned {total_events} events, found {len(output_contacts)} contacts.", file=sys.stderr)


if __name__ == "__main__":
    mine_calendar()
