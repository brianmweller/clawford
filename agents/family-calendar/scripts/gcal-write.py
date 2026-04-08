#!/usr/bin/env python3
"""
gcal-write.py — Create, update, and delete Google Calendar events.

Used by Mistress Mouse to manage calendar events after human confirmation.
All operations require explicit --confirm flag to prevent accidental writes.

Usage:
  python3 gcal-write.py create --calendar-id X --summary "Dentist" --start "2026-04-10T14:00" --end "2026-04-10T15:00" [--location "..."] --confirm
  python3 gcal-write.py move --calendar-id X --event-id Y --new-start "2026-04-11T15:00" --new-end "2026-04-11T16:00" --confirm
  python3 gcal-write.py remove --calendar-id X --event-id Y --confirm
  python3 gcal-write.py preview-create --calendar-id X --summary "Dentist" --start "2026-04-10T14:00" --end "2026-04-10T15:00"

Preview commands (no --confirm) return what WOULD happen without making changes.

Output JSON:
  {"status": "ok", "action": "created", "event_id": "...", "summary": "..."}
  {"status": "ok", "action": "moved", "event_id": "...", "old_start": "...", "new_start": "..."}
  {"status": "ok", "action": "removed", "event_id": "...", "summary": "..."}
  {"status": "preview", "action": "would_create", ...}

Requires: google-api-python-client with calendar (full) scope
"""

import json
import os
import sys
from datetime import datetime, timezone

WORKSPACE = os.path.expanduser("~/.openclaw/family-calendar-workspace")
TOKEN_PATH = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(WORKSPACE, "token.json"),
)
CONFIG_PATH = os.path.join(WORKSPACE, "calendar-config.json")
LOG_PATH = os.path.join(WORKSPACE, "logs", "calendar-writes.jsonl")


def parse_args():
    args = {
        "action": None,
        "calendar_id": None,
        "event_id": None,
        "summary": None,
        "start": None,
        "end": None,
        "new_start": None,
        "new_end": None,
        "location": None,
        "confirm": False,
    }

    if len(sys.argv) < 2:
        return args

    args["action"] = sys.argv[1]

    i = 2
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--calendar-id" and i + 1 < len(sys.argv):
            args["calendar_id"] = sys.argv[i + 1]
            i += 2
        elif arg == "--event-id" and i + 1 < len(sys.argv):
            args["event_id"] = sys.argv[i + 1]
            i += 2
        elif arg == "--summary" and i + 1 < len(sys.argv):
            args["summary"] = sys.argv[i + 1]
            i += 2
        elif arg == "--start" and i + 1 < len(sys.argv):
            args["start"] = sys.argv[i + 1]
            i += 2
        elif arg == "--end" and i + 1 < len(sys.argv):
            args["end"] = sys.argv[i + 1]
            i += 2
        elif arg == "--new-start" and i + 1 < len(sys.argv):
            args["new_start"] = sys.argv[i + 1]
            i += 2
        elif arg == "--new-end" and i + 1 < len(sys.argv):
            args["new_end"] = sys.argv[i + 1]
            i += 2
        elif arg == "--location" and i + 1 < len(sys.argv):
            args["location"] = sys.argv[i + 1]
            i += 2
        elif arg == "--confirm":
            args["confirm"] = True
            i += 1
        else:
            i += 1

    return args


def get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

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


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return None, "calendar-config.json not found"
    with open(CONFIG_PATH) as f:
        return json.load(f), None


def resolve_calendar_id(config, cal_input):
    """Resolve a label like 'Sam' or 'Alex' to a calendar ID."""
    if not cal_input:
        # Default to first calendar
        cals = config.get("calendars", [])
        return cals[0]["id"] if cals else None

    # Try exact match on ID
    for cal in config.get("calendars", []):
        if cal["id"] == cal_input:
            return cal_input

    # Try label match (case-insensitive)
    for cal in config.get("calendars", []):
        if cal["label"].lower() == cal_input.lower():
            return cal["id"]

    return cal_input  # Pass through as-is


def normalize_datetime(dt_str, tz_name="America/Los_Angeles"):
    """Ensure datetime string has timezone info."""
    if not dt_str:
        return None
    # Already has timezone
    if "+" in dt_str or "Z" in dt_str or "-" in dt_str[11:]:
        return dt_str
    # Add timezone
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M")
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        return dt.isoformat()
    except (ImportError, ValueError):
        return dt_str + ":00"


def log_action(action_data):
    """Append to write log for audit trail."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    action_data["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(action_data) + "\n")


def error(msg):
    result = {"status": "error", "message": msg}
    print(json.dumps(result))
    sys.exit(1)


def main():
    args = parse_args()

    if not args["action"]:
        error("Usage: gcal-write.py <create|move|remove|preview-create> [options]")

    config, err = load_config()
    if err:
        error(err)

    tz_name = config.get("timezone", "America/Los_Angeles")
    action = args["action"]

    # Preview modes don't need credentials
    if action.startswith("preview-"):
        actual_action = action.replace("preview-", "")
        if actual_action == "create":
            cal_id = resolve_calendar_id(config, args["calendar_id"])
            cal_label = next(
                (c["label"] for c in config.get("calendars", []) if c["id"] == cal_id),
                cal_id,
            )
            result = {
                "status": "preview",
                "action": "would_create",
                "calendar": cal_label,
                "summary": args["summary"],
                "start": normalize_datetime(args["start"], tz_name),
                "end": normalize_datetime(args["end"], tz_name),
                "location": args["location"] or "",
            }
            print(json.dumps(result, indent=2))
            return
        error(f"Unknown preview action: {action}")

    # Real actions require --confirm
    if not args["confirm"]:
        error(f"Action '{action}' requires --confirm flag. Use preview-{action} to see what would happen.")

    creds, err = get_credentials()
    if err:
        error(err)

    try:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds)
    except Exception as e:
        error(f"Failed to build Calendar service: {e}")

    cal_id = resolve_calendar_id(config, args["calendar_id"])
    cal_label = next(
        (c["label"] for c in config.get("calendars", []) if c["id"] == cal_id),
        cal_id,
    )

    if action == "create":
        if not args["summary"] or not args["start"]:
            error("create requires --summary and --start")

        event_body = {
            "summary": args["summary"],
            "start": {"dateTime": normalize_datetime(args["start"], tz_name)},
            "end": {"dateTime": normalize_datetime(args["end"] or args["start"], tz_name)},
        }
        if args["location"]:
            event_body["location"] = args["location"]

        event = service.events().insert(calendarId=cal_id, body=event_body).execute()

        result = {
            "status": "ok",
            "action": "created",
            "event_id": event["id"],
            "calendar": cal_label,
            "summary": args["summary"],
            "start": event["start"].get("dateTime", event["start"].get("date")),
        }
        log_action(result)
        print(json.dumps(result, indent=2))

    elif action == "move":
        if not args["event_id"]:
            error("move requires --event-id")
        if not args["new_start"]:
            error("move requires --new-start")

        # Get current event
        event = service.events().get(calendarId=cal_id, eventId=args["event_id"]).execute()
        old_start = event["start"].get("dateTime", event["start"].get("date"))

        # Update
        event["start"]["dateTime"] = normalize_datetime(args["new_start"], tz_name)
        if args["new_end"]:
            event["end"]["dateTime"] = normalize_datetime(args["new_end"], tz_name)

        updated = service.events().update(
            calendarId=cal_id, eventId=args["event_id"], body=event
        ).execute()

        result = {
            "status": "ok",
            "action": "moved",
            "event_id": args["event_id"],
            "calendar": cal_label,
            "summary": updated.get("summary", ""),
            "old_start": old_start,
            "new_start": normalize_datetime(args["new_start"], tz_name),
        }
        log_action(result)
        print(json.dumps(result, indent=2))

    elif action == "remove":
        if not args["event_id"]:
            error("remove requires --event-id")

        # Get event details before deleting (for logging)
        event = service.events().get(calendarId=cal_id, eventId=args["event_id"]).execute()
        summary = event.get("summary", "(No title)")

        service.events().delete(calendarId=cal_id, eventId=args["event_id"]).execute()

        result = {
            "status": "ok",
            "action": "removed",
            "event_id": args["event_id"],
            "calendar": cal_label,
            "summary": summary,
        }
        log_action(result)
        print(json.dumps(result, indent=2))

    else:
        error(f"Unknown action: {action}")


if __name__ == "__main__":
    main()
