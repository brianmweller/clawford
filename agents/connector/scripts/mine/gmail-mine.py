#!/usr/bin/env python3
"""
gmail-mine.py — Mine 2 years of Gmail for contacts, signatures, and interaction data.

Fetches full message bodies. Extracts sender/recipient pairs, subjects,
body excerpts, and signature blocks (job title, company, phone, LinkedIn).
Aggregates per email address.

Usage:
  python3 gmail-mine.py                  # Mine last 2 years
  python3 gmail-mine.py --resume         # Resume from checkpoint
  python3 gmail-mine.py --fresh          # Ignore checkpoint, start over

Output: cache/mined-gmail.json
"""

import base64
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

# Add parent to path for mining_utils
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_utils import (
    get_google_credentials,
    is_brian,
    is_noreply,
    load_checkpoint,
    load_config,
    normalize_email,
    parse_email_header,
    parse_email_header_list,
    parse_signature,
    save_checkpoint,
    save_mined,
)

DEFAULT_TOKEN_PATHS = [
    os.path.expanduser("~/.openclaw/family-calendar-workspace/token.json"),
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "family-calendar", "token.json"),
]


def find_token():
    """Find a working Gmail OAuth token."""
    config = load_config()
    custom = config.get("gmail", {}).get("token_path")
    if custom and os.path.exists(custom):
        return custom
    for path in DEFAULT_TOKEN_PATHS:
        if os.path.exists(path):
            return path
    return None


def decode_body(payload):
    """Decode a Gmail message payload to plain text."""
    # Try text/plain first
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Multipart: recurse
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Fallback: try HTML
    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                # Crude HTML strip
                import re
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text)
                return text[:5000]

    # Nested multipart
    for part in parts:
        result = decode_body(part)
        if result:
            return result

    return ""


def get_header(headers, name):
    """Get a header value by name from Gmail message headers."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def mine_gmail():
    config = load_config()
    gmail_config = config.get("gmail", {})
    batch_size = gmail_config.get("batch_size", 50)
    batch_delay = gmail_config.get("batch_delay_seconds", 1.0)
    max_body = gmail_config.get("max_body_chars", 3000)
    lookback = config.get("lookback_years", 2)

    token_path = find_token()
    if not token_path:
        print("ERROR: No Gmail token found. Check token paths.", file=sys.stderr)
        sys.exit(1)

    print(f"Using token: {token_path}", file=sys.stderr)

    creds, err = get_google_credentials(token_path)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=creds)

    # Check for resume
    resume = "--resume" in sys.argv
    fresh = "--fresh" in sys.argv
    checkpoint = None
    if resume and not fresh:
        checkpoint = load_checkpoint("gmail")
        if checkpoint:
            print(f"Resuming from checkpoint: {checkpoint.get('messages_processed', 0)} messages done", file=sys.stderr)

    contacts = defaultdict(lambda: {
        "email": "",
        "display_names": set(),
        "sent_count": 0,
        "received_count": 0,
        "first_seen": None,
        "last_seen": None,
        "subjects": [],
        "body_excerpts": [],
        "signatures": [],
        "is_noreply": False,
    })

    # Restore contacts from checkpoint
    if checkpoint:
        for email, data in checkpoint.get("contacts", {}).items():
            contacts[email] = data
            contacts[email]["display_names"] = set(data.get("display_names", []))

    query = f"newer_than:{lookback}y"
    page_token = checkpoint.get("next_page_token") if checkpoint else None
    total_processed = checkpoint.get("messages_processed", 0) if checkpoint else 0
    total_fetched = 0

    print(f"Mining Gmail: query='{query}', batch_size={batch_size}", file=sys.stderr)

    while True:
        # List messages
        list_args = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            list_args["pageToken"] = page_token

        try:
            result = service.users().messages().list(**list_args).execute()
        except Exception as e:
            print(f"ERROR listing messages: {e}", file=sys.stderr)
            break

        messages = result.get("messages", [])
        if not messages:
            break

        # Process in batches
        for i in range(0, len(messages), batch_size):
            batch = messages[i:i + batch_size]

            for msg_stub in batch:
                msg_id = msg_stub["id"]
                try:
                    msg = service.users().messages().get(
                        userId="me", id=msg_id, format="full"
                    ).execute()
                except Exception as e:
                    print(f"  SKIP {msg_id}: {e}", file=sys.stderr)
                    continue

                headers = msg.get("payload", {}).get("headers", [])
                from_header = get_header(headers, "From")
                to_header = get_header(headers, "To")
                cc_header = get_header(headers, "Cc")
                date_header = get_header(headers, "Date")
                subject = get_header(headers, "Subject")

                # Determine direction
                from_name, from_email = parse_email_header(from_header)
                is_sent = is_brian(from_email, config)

                # Decode body
                body = decode_body(msg.get("payload", {}))
                body_excerpt = body[:max_body] if body else ""

                # Parse signature from body
                sig = parse_signature(body) if body else {}

                # Parse date
                msg_date = None
                if date_header:
                    try:
                        from email.utils import parsedate_to_datetime
                        msg_date = parsedate_to_datetime(date_header).strftime("%Y-%m-%d")
                    except Exception:
                        pass

                # Collect contacts
                if is_sent:
                    # Sam sent this — recipients are contacts
                    recipients = parse_email_header_list(to_header) + parse_email_header_list(cc_header)
                    for name, email in recipients:
                        email = normalize_email(email)
                        if not email or is_brian(email, config):
                            continue
                        c = contacts[email]
                        c["email"] = email
                        if name:
                            c["display_names"].add(name)
                        c["sent_count"] += 1
                        if msg_date:
                            if not c["first_seen"] or msg_date < c["first_seen"]:
                                c["first_seen"] = msg_date
                            if not c["last_seen"] or msg_date > c["last_seen"]:
                                c["last_seen"] = msg_date
                        if subject and len(c["subjects"]) < 50:
                            c["subjects"].append(subject)
                else:
                    # Sam received this — sender is the contact
                    email = normalize_email(from_email)
                    if email and not is_brian(email, config):
                        c = contacts[email]
                        c["email"] = email
                        if from_name:
                            c["display_names"].add(from_name)
                        c["received_count"] += 1
                        if msg_date:
                            if not c["first_seen"] or msg_date < c["first_seen"]:
                                c["first_seen"] = msg_date
                            if not c["last_seen"] or msg_date > c["last_seen"]:
                                c["last_seen"] = msg_date
                        if subject and len(c["subjects"]) < 50:
                            c["subjects"].append(subject)
                        if body_excerpt and len(c["body_excerpts"]) < 10:
                            c["body_excerpts"].append(body_excerpt)
                        if sig and len(c["signatures"]) < 5:
                            c["signatures"].append(sig)

                total_processed += 1
                total_fetched += 1

            # Progress
            print(f"  Processed {total_processed} messages ({len(contacts)} contacts)", file=sys.stderr)

            # Checkpoint every batch
            save_checkpoint("gmail", {
                "messages_processed": total_processed,
                "next_page_token": result.get("nextPageToken"),
                "contacts": {
                    email: {**data, "display_names": list(data["display_names"])}
                    for email, data in contacts.items()
                },
            })

            # Rate limit
            time.sleep(batch_delay)

            # Refresh credentials periodically
            if total_fetched % 500 == 0:
                creds, err = get_google_credentials(token_path)
                if err:
                    print(f"WARNING: Token refresh failed: {err}", file=sys.stderr)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    # Finalize: pick best signature per contact, convert sets to lists
    output_contacts = {}
    for email, data in contacts.items():
        data["display_names"] = list(data["display_names"])
        data["is_noreply"] = is_noreply(email, config)

        # Pick best signature: most recent with the most fields
        if data["signatures"]:
            best = max(data["signatures"], key=lambda s: len(s))
            data["signature"] = best
        else:
            data["signature"] = {}

        # Dedupe subjects
        seen = set()
        unique_subjects = []
        for s in data["subjects"]:
            key = s.lower().strip()
            if key not in seen:
                seen.add(key)
                unique_subjects.append(s)
        data["subjects"] = unique_subjects[:30]

        del data["signatures"]  # Don't need the raw list
        output_contacts[email] = data

    result = {
        "status": "ok",
        "source": "gmail",
        "mined_at": datetime.now(timezone.utc).isoformat(),
        "messages_scanned": total_processed,
        "contacts_found": len(output_contacts),
        "contacts": output_contacts,
    }

    save_mined("gmail", result)
    print(f"\nDone. Scanned {total_processed} messages, found {len(output_contacts)} contacts.", file=sys.stderr)


if __name__ == "__main__":
    mine_gmail()
