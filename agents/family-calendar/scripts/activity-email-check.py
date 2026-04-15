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

WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")
TOKEN_PATH = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(WORKSPACE, "token.json"),
)
CACHE_DIR = os.path.join(WORKSPACE, "cache")
SEEN_PATH = os.path.join(CACHE_DIR, "seen-activity-emails.json")
CONFIG_PATH = os.path.join(WORKSPACE, "calendar-config.json")

# Sanitized fallback. Real provider names are PII and must not live in
# the tracked script — they load from calendar-config.json at runtime.
DEFAULT_PROVIDERS = [
    {"name": "Example Preschool", "query": "from:ExamplePreschool newer_than:2d"},
    {"name": "Example Swim School", "query": "from:ExampleSwim newer_than:2d"},
    {"name": "Example Ballet Studio", "query": "from:ExampleBallet newer_than:2d"},
]


def load_providers(config_path=CONFIG_PATH):
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return DEFAULT_PROVIDERS
    providers = config.get("activity_providers")
    if not isinstance(providers, list) or not providers:
        return DEFAULT_PROVIDERS
    return providers


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


# A text/plain part shorter than this is assumed to be a "please view
# in HTML" stub (Smore, Mailchimp, and similar senders) — fall through
# to the text/html part instead. Real newsletter plaintext runs into
# the thousands of characters, so 100 is comfortably below any real
# content and well above typical stubs (4–50 chars).
PLAIN_STUB_THRESHOLD = 100


def _iter_leaves(part):
    """Yield every leaf MIME part in depth-first order.

    A payload without a `parts` array is itself a leaf — this is how
    top-level text/html emails (common from Tutu School's mailer) get
    reached. Containers like multipart/mixed recurse into their
    children.
    """
    children = part.get("parts") or []
    if not children:
        yield part
        return
    for child in children:
        yield from _iter_leaves(child)


def _decode_body(part):
    data = (part.get("body") or {}).get("data") or ""
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def get_email_body(msg):
    """Extract readable body text from a Gmail message.

    Prefers text/plain when it carries real content; falls through to
    HTML-stripped text/html when text/plain is absent or a stub. Walks
    multipart containers recursively so nested structures
    (multipart/mixed → multipart/alternative → plain+html) reach their
    leaves.
    """
    payload = msg.get("payload") or {}
    plain = ""
    html = ""
    for leaf in _iter_leaves(payload):
        mime = leaf.get("mimeType", "")
        if mime == "text/plain" and not plain:
            plain = _decode_body(leaf)
        elif mime == "text/html" and not html:
            html = _decode_body(leaf)

    if len(plain.strip()) >= PLAIN_STUB_THRESHOLD:
        return plain
    if html:
        return re.sub(r"<[^>]+>", " ", html)
    return plain


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

    for provider in load_providers():
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
