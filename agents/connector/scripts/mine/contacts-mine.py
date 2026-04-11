#!/usr/bin/env python3
"""
contacts-mine.py — Export Google Contacts as an email→name+phone lookup table.

Fetches all contacts via the People API. Builds a map from email address
to display name and phone number. Used by the aggregator to resolve
email-only contacts to real names.

Usage:
  python3 contacts-mine.py

Output: cache/mined-contacts.json
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import get_google_credentials, load_config, normalize_email, save_mined

DEFAULT_TOKEN_PATHS = [
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "family-calendar", "token.json"),
    os.path.expanduser("~/.openclaw/family-calendar-workspace/token.json"),
]


def find_token():
    config = load_config()
    custom = config.get("contacts", {}).get("token_path")
    if custom and os.path.exists(custom):
        return custom
    for path in DEFAULT_TOKEN_PATHS:
        if os.path.exists(path):
            return path
    return None


def mine_contacts():
    token_path = find_token()
    if not token_path:
        print("ERROR: No token found with contacts.readonly scope.", file=sys.stderr)
        sys.exit(1)

    print(f"Using token: {token_path}", file=sys.stderr)
    creds, err = get_google_credentials(token_path)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    from googleapiclient.discovery import build
    service = build("people", "v1", credentials=creds)

    # Build email→{name, phone} lookup from BOTH saved and "other" contacts
    email_to_name = {}
    phone_to_name = {}
    total = 0

    # 1. Saved contacts (people.connections) — higher quality names
    page_token = None
    print("Fetching saved Google Contacts...", file=sys.stderr)
    while True:
        result = service.people().connections().list(
            resourceName="people/me",
            pageSize=100,
            personFields="names,emailAddresses,phoneNumbers",
            pageToken=page_token,
        ).execute()

        for person in result.get("connections", []):
            total += 1
            names = person.get("names", [])
            emails = person.get("emailAddresses", [])
            phones = person.get("phoneNumbers", [])

            display_name = names[0].get("displayName", "") if names else ""
            if not display_name:
                continue

            for email_entry in emails:
                email = normalize_email(email_entry.get("value", ""))
                if email:
                    email_to_name[email] = {
                        "name": display_name,
                        "phone": phones[0].get("value", "") if phones else "",
                        "source": "saved",
                    }

            for phone_entry in phones:
                phone = phone_entry.get("value", "").strip()
                if phone and phone not in phone_to_name:
                    phone_to_name[phone] = {
                        "name": display_name,
                        "email": emails[0].get("value", "") if emails else "",
                    }

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    saved_count = total
    print(f"  Saved contacts: {saved_count}", file=sys.stderr)

    # 2. Other contacts (auto-saved from interactions) — fill gaps only
    page_token = None
    other_count = 0
    print("Fetching auto-saved 'Other' contacts...", file=sys.stderr)
    while True:
        try:
            result = service.otherContacts().list(
                pageSize=100,
                readMask="names,emailAddresses,phoneNumbers",
                pageToken=page_token,
            ).execute()
        except Exception as e:
            print(f"  Other contacts unavailable: {e}", file=sys.stderr)
            break

        for person in result.get("otherContacts", []):
            total += 1
            other_count += 1
            names = person.get("names", [])
            emails = person.get("emailAddresses", [])
            phones = person.get("phoneNumbers", [])

            display_name = names[0].get("displayName", "") if names else ""
            if not display_name:
                continue

            for email_entry in emails:
                email = normalize_email(email_entry.get("value", ""))
                if email and email not in email_to_name:
                    email_to_name[email] = {
                        "name": display_name,
                        "phone": phones[0].get("value", "") if phones else "",
                        "source": "other",
                    }

            for phone_entry in phones:
                phone = phone_entry.get("value", "").strip()
                if phone and phone not in phone_to_name:
                    phone_to_name[phone] = {
                        "name": display_name,
                        "email": emails[0].get("value", "") if emails else "",
                    }

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    print(f"  Other contacts: {other_count}", file=sys.stderr)

    result = {
        "status": "ok",
        "source": "google-contacts",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "total_contacts": total,
        "email_mappings": len(email_to_name),
        "phone_mappings": len(phone_to_name),
        "email_to_name": email_to_name,
        "phone_to_name": phone_to_name,
    }

    save_mined("contacts", result)
    print(f"\nDone. {total} contacts → {len(email_to_name)} email mappings, {len(phone_to_name)} phone mappings.", file=sys.stderr)


if __name__ == "__main__":
    mine_contacts()
