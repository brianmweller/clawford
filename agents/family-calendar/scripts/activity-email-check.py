#!/usr/bin/env python3
"""
activity-email-check.py — Fetch emails from Avery's activity providers.

Searches Gmail for emails from Example Preschool (school), Example Swim School (swimming),
and Example Ballet Studio (ballet). Returns raw email content as JSON
for the agent's LLM to parse.

Usage: python3 activity-email-check.py [--hours N]

Output JSON:
  [
    {
      "source": "Example Preschool",
      "subject": "Weekly Newsletter - Room 3",
      "body": "Dear Room 3 families...",
      "from": "newsletter@ExamplePreschool.com",
      "date": "2026-04-08",
      "message_id": "..."
    }
  ]

The agent's LLM parses these for action items, closures, cancellations.
This script does I/O only — no LLM calls.

Requires: google-api-python-client (gmail.readonly scope)
"""

import base64
import json
import os
import re
import sys
from datetime import datetime, timezone

WORKSPACE = os.path.expanduser("~/.openclaw/family-calendar-workspace")
TOKEN_PATH = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(WORKSPACE, "token.json"),
)
CACHE_DIR = os.path.join(WORKSPACE, "cache")
SEEN_PATH = os.path.join(CACHE_DIR, "seen-activity-emails.json")

PROVIDERS = [
    {"name": "Example Preschool", "query": "from:ExamplePreschool newer_than:2d"},
    {"name": "Example Swim School", "query": "from:ExampleSwim newer_than:2d"},
    {"name": "Example Ballet Studio", "query": "(from:tutu OR from:tutuschool) newer_than:2d"},
]


def parse_args():
    hours = 48
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
    seen_list = list(seen)[-500:]
    with open(SEEN_PATH, "w") as f:
        json.dump(seen_list, f)


def get_email_body(msg):
    """Extract plain text body from Gmail message."""
    payload = msg.get("payload", {})

    if payload.get("mimeType", "").startswith("text/plain"):
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", html)[:3000]

    return ""


def main():
    hours = parse_args()

    creds, err = get_credentials()
    if err:
        print(json.dumps({"status": "error", "message": err}), file=sys.stderr)
        print(json.dumps([]))
        return

    try:
        from googleapiclient.discovery import build
        gmail = build("gmail", "v1", credentials=creds)
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}), file=sys.stderr)
        print(json.dumps([]))
        return

    seen = load_seen()
    results = []

    for provider in PROVIDERS:
        try:
            search = gmail.users().messages().list(
                userId="me", q=provider["query"], maxResults=10
            ).execute()

            for msg_stub in search.get("messages", []):
                msg_id = msg_stub["id"]
                if msg_id in seen:
                    continue

                msg = gmail.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()

                headers = {h["name"].lower(): h["value"]
                           for h in msg.get("payload", {}).get("headers", [])}
                subject = headers.get("subject", "")
                from_addr = headers.get("from", "")
                date_str = headers.get("date", "")
                body = get_email_body(msg)

                if not body.strip():
                    seen.add(msg_id)
                    continue

                results.append({
                    "source": provider["name"],
                    "subject": subject,
                    "from": from_addr,
                    "date": date_str,
                    "body": body[:3000],
                    "message_id": msg_id,
                })

                seen.add(msg_id)

        except Exception as e:
            print(f"Provider {provider['name']} failed: {e}", file=sys.stderr)

    save_seen(seen)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
