#!/usr/bin/env python3
"""
reminder-check.py — Poll for upcoming events and emit reminders.

Checks all configured Google Calendars for events in the next 60 minutes.
Deduplicates against sent-reminders.json to avoid re-sending.
Outputs JSON array of reminders to send (or empty array if none).

Usage: python3 reminder-check.py

Output JSON (to stdout):
  [
    {
      "event_id": "...",
      "summary": "Dentist",
      "calendar_label": "Sam",
      "calendar_emoji": "👨",
      "starts_in_min": 30,
      "location": "123 Main St",
      "tier": "30min"
    }
  ]

Reminder tiers:
  - 60min: events with location containing travel keywords
  - 30min: standard events (default)
  - 15min: pickup/dropoff events

Dedup key: {event_id}_{calendar_id}_{tier}
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

WORKSPACE = os.path.expanduser("~/.openclaw/family-calendar-workspace")
REMINDERS_PATH = os.path.join(WORKSPACE, "sent-reminders.json")
CONFIG_PATH = os.path.join(WORKSPACE, "calendar-config.json")
TOKEN_PATH = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(WORKSPACE, "token.json"),
)

# Mistress Mouse / Sergeant Murphy routing boundary: events present in
# Murphy's workflowy-links.json are "meetings" (his domain). Mistress
# Mouse must NOT fire reminders for those — Murphy owns them via
# pre-meeting-alert. See memory: project_meeting_event_routing.md.
WORKFLOWY_LINKS_PATH = os.environ.get(
    "WORKFLOWY_LINKS_PATH",
    os.path.expanduser(
        "~/.openclaw/meetings-coach-workspace/cache/workflowy-links.json"
    ),
)


def load_workflowy_linked_event_ids() -> set:
    """Return the set of GCal event IDs that have a Workflowy link.

    File-missing → empty set (degrade open: if Murphy hasn't recorded
    any meetings yet, don't over-filter).
    """
    if not os.path.exists(WORKFLOWY_LINKS_PATH):
        return set()
    try:
        with open(WORKFLOWY_LINKS_PATH) as f:
            data = json.load(f)
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    return set(data.keys())

TRAVEL_KEYWORDS = re.compile(
    r"airport|doctor|dentist|hospital|clinic|urgent care|emergency",
    re.IGNORECASE,
)
PICKUP_KEYWORDS = re.compile(
    r"pickup|pick up|pick-up|drop off|drop-off|dropoff|school pickup|school drop",
    re.IGNORECASE,
)


def load_sent_reminders():
    if not os.path.exists(REMINDERS_PATH):
        return {"reminders": {}, "last_pruned": None}
    with open(REMINDERS_PATH) as f:
        return json.load(f)


def save_sent_reminders(data):
    with open(REMINDERS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def prune_old_reminders(data):
    """Remove entries older than 48 hours."""
    now = datetime.now(timezone.utc)
    last_pruned = data.get("last_pruned")

    if last_pruned:
        try:
            lp = datetime.fromisoformat(last_pruned)
            if (now - lp).total_seconds() < 86400:  # Less than 24h since last prune
                return data
        except (ValueError, TypeError):
            pass

    cutoff = (now - timedelta(hours=48)).isoformat()
    reminders = data.get("reminders", {})
    data["reminders"] = {k: v for k, v in reminders.items() if v > cutoff}
    data["last_pruned"] = now.isoformat()
    return data


def classify_tier(summary, location):
    """Determine reminder tier based on event content."""
    text = f"{summary} {location}"
    if TRAVEL_KEYWORDS.search(text):
        return "60min", 60
    if PICKUP_KEYWORDS.search(summary):
        return "15min", 15
    return "30min", 30


def get_credentials():
    """Load OAuth2 credentials."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None, "google-auth not installed"

    if not os.path.exists(TOKEN_PATH):
        return None, "token.json not found"

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
            token_data["token"] = creds.token
            with open(TOKEN_PATH, "w") as f:
                json.dump(token_data, f, indent=2)
        except Exception as e:
            return None, f"Token refresh failed: {e}"

    return creds, None


def main():
    # Load config
    if not os.path.exists(CONFIG_PATH):
        print(json.dumps([]))
        sys.exit(0)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    # Get credentials
    creds, err = get_credentials()
    if err:
        print(json.dumps({"status": "error", "message": err}), file=sys.stderr)
        print(json.dumps([]))
        sys.exit(1)

    # Build service
    try:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds)
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}), file=sys.stderr)
        print(json.dumps([]))
        sys.exit(1)

    # Time window: now to now+60min
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(minutes=60)).isoformat()

    # Load sent reminders
    sent_data = load_sent_reminders()
    sent_data = prune_old_reminders(sent_data)
    sent_reminders = sent_data.get("reminders", {})

    # Mistress Mouse / Sergeant Murphy routing boundary: load Murphy's
    # workflowy-links.json and skip reminders for any event that has a
    # Workflowy item (those are Murphy's pre-meeting-alert domain).
    workflowy_linked_ids = load_workflowy_linked_event_ids()

    # Fetch events from all calendars
    reminders_to_send = []

    for cal in config.get("calendars", []):
        if not cal.get("remind", True):
            continue

        cal_id = cal["id"]
        label = cal["label"]
        emoji = cal.get("emoji", "")

        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            ).execute()

            for event in result.get("items", []):
                if event.get("status") == "cancelled":
                    continue

                start = event.get("start", {})
                if "date" in start:  # Skip all-day events
                    continue

                event_id = event.get("id", "")

                # Routing boundary: Murphy owns Workflowy-linked events.
                if event_id in workflowy_linked_ids:
                    continue

                summary = event.get("summary", "(No title)")
                location = event.get("location", "")
                start_time = start.get("dateTime", "")

                if not start_time:
                    continue

                # Calculate minutes until event
                try:
                    event_start = datetime.fromisoformat(start_time)
                    minutes_until = (event_start - now).total_seconds() / 60
                except (ValueError, TypeError):
                    continue

                if minutes_until < 0:
                    continue

                # Determine tier
                tier, tier_minutes = classify_tier(summary, location)

                # Check if this reminder should fire
                if minutes_until > tier_minutes:
                    continue

                # Check dedup
                dedup_key = f"{event_id}_{cal_id}_{tier}"
                if dedup_key in sent_reminders:
                    continue

                reminders_to_send.append({
                    "event_id": event_id,
                    "summary": summary,
                    "calendar_label": label,
                    "calendar_emoji": emoji,
                    "starts_in_min": round(minutes_until),
                    "location": location,
                    "tier": tier,
                    "_dedup_key": dedup_key,
                })

        except Exception as e:
            print(f"Error fetching {label}: {e}", file=sys.stderr)

    # Mark reminders as sent
    for reminder in reminders_to_send:
        key = reminder.pop("_dedup_key")
        sent_reminders[key] = now.isoformat()

    # Save updated sent reminders
    sent_data["reminders"] = sent_reminders
    save_sent_reminders(sent_data)

    # Output
    print(json.dumps(reminders_to_send, indent=2))


if __name__ == "__main__":
    main()
