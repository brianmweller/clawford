#!/usr/bin/env python3
"""
gmail-invite-check.py — Check Gmail for calendar invites.

Searches for ICS attachments and Google Calendar notification emails.
Parses invite details and outputs JSON for the agent to surface on Telegram.

Usage: python3 gmail-invite-check.py [--hours N]

Output JSON:
  [
    {
      "subject": "Team Offsite",
      "organizer": "alice@company.com",
      "start": "2026-04-15T09:00:00-07:00",
      "end": "2026-04-15T17:00:00-07:00",
      "location": "123 Main St",
      "status": "needsAction",
      "message_id": "...",
      "received_at": "2026-04-08T10:00:00Z"
    }
  ]

Requires: google-api-python-client with gmail.readonly scope
"""

import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser

WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")
TOKEN_PATH = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(WORKSPACE, "token.json"),
)
CACHE_DIR = os.path.join(WORKSPACE, "cache")
SEEN_PATH = os.path.join(CACHE_DIR, "seen-invites.json")


def parse_args():
    hours = 24
    for i, arg in enumerate(sys.argv):
        if arg == "--hours" and i + 1 < len(sys.argv):
            hours = int(sys.argv[i + 1])
    return hours


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


def load_seen():
    if not os.path.exists(SEEN_PATH):
        return set()
    with open(SEEN_PATH) as f:
        return set(json.load(f))


def save_seen(seen):
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Keep only last 500 entries
    seen_list = list(seen)[-500:]
    with open(SEEN_PATH, "w") as f:
        json.dump(seen_list, f)


def parse_ics_basic(ics_text):
    """Extract basic event info from ICS text without icalendar library."""
    result = {}
    for line in ics_text.split("\n"):
        line = line.strip()
        if line.startswith("SUMMARY:"):
            result["subject"] = line[8:]
        elif line.startswith("DTSTART"):
            val = line.split(":", 1)[-1]
            result["start"] = val
        elif line.startswith("DTEND"):
            val = line.split(":", 1)[-1]
            result["end"] = val
        elif line.startswith("LOCATION:"):
            result["location"] = line[9:]
        elif line.startswith("ORGANIZER"):
            # ORGANIZER;CN=Name:mailto:email
            match = re.search(r"mailto:(.+)", line)
            if match:
                result["organizer"] = match.group(1)
        elif line.startswith("STATUS:"):
            result["status"] = line[7:]
        elif line.startswith("METHOD:"):
            result["method"] = line[7:]
    return result


# Noise filters — Gmail queries also return daily digests and acceptance
# notifications that aren't new invites. Skip anything matching these.
NOISE_SUBJECT_PREFIXES = (
    "daily agenda",
    "agenda for",
    "your daily agenda",
    "accepted:",
    "declined:",
    "tentative:",
    "updated invitation:",  # these are reschedules — could surface but noisy
    "canceled event:",
    "cancelled event:",
)


def is_noise(subject):
    """Return True if this subject is not an actionable new invite."""
    if not subject:
        return True
    subj_lower = subject.lower().strip()
    for prefix in NOISE_SUBJECT_PREFIXES:
        if subj_lower.startswith(prefix):
            return True
    return False


def main():
    hours = parse_args()

    creds, err = get_credentials()
    if err:
        print(json.dumps({"status": "error", "message": err}), file=sys.stderr)
        print(json.dumps([]))
        return

    try:
        from googleapiclient.discovery import build
        service = build("gmail", "v1", credentials=creds)
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}), file=sys.stderr)
        print(json.dumps([]))
        return

    seen = load_seen()
    invites = []

    # Narrower query: only ICS attachments, not the full calendar-notification
    # stream (which includes daily agendas and acceptance notifications).
    # ICS attachments come with invitations AND responses — we filter the
    # responses out via METHOD and subject prefix.
    query = "has:attachment filename:ics newer_than:1d"

    try:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=20
        ).execute()

        for msg_stub in results.get("messages", []):
            msg_id = msg_stub["id"]
            if msg_id in seen:
                continue

            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()

            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            subject = headers.get("subject", "")
            from_addr = headers.get("from", "")
            date_str = headers.get("date", "")

            # Filter 1: skip noise subjects (daily agendas, accepted, declined, etc.)
            if is_noise(subject):
                seen.add(msg_id)
                continue

            # Check for ICS attachments
            ics_data = None
            parts = msg.get("payload", {}).get("parts", [])
            for part in parts:
                filename = part.get("filename", "")
                if filename.endswith(".ics"):
                    att_id = part.get("body", {}).get("attachmentId")
                    if att_id:
                        att = service.users().messages().attachments().get(
                            userId="me", messageId=msg_id, id=att_id
                        ).execute()
                        ics_bytes = base64.urlsafe_b64decode(att["data"])
                        ics_data = ics_bytes.decode("utf-8", errors="replace")

            # Filter 2: only surface METHOD:REQUEST (new invites) —
            # skip REPLY (acceptance), CANCEL, COUNTER, REFRESH
            if ics_data:
                parsed = parse_ics_basic(ics_data)
                method = parsed.get("method", "").upper()
                if method and method != "REQUEST":
                    seen.add(msg_id)
                    continue
            else:
                # No ICS attachment at all — probably not a real invite
                seen.add(msg_id)
                continue

            invite = {
                "subject": subject,
                "from": from_addr,
                "message_id": msg_id,
                "received_at": date_str,
            }
            invite.update(parsed)

            invites.append(invite)
            seen.add(msg_id)

    except Exception as e:
        print(f"Query '{query}' failed: {e}", file=sys.stderr)

    save_seen(seen)
    print(json.dumps(invites, indent=2))


if __name__ == "__main__":
    main()
