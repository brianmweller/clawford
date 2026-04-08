#!/usr/bin/env python3
"""
activity-email-check.py — Monitor emails from Avery's activity providers.

Searches Gmail for emails from Example Preschool (school), Example Swim School (swimming),
and Example Ballet Studio (ballet). Uses LLM (gpt-5.4-nano) to parse
for schedule changes, cancellations, closures, and action items
(Room 3 needs to bring/prep/wear something).

Usage: python3 activity-email-check.py [--hours N] [--dry-run]

Output JSON:
  [
    {
      "source": "Example Preschool",
      "type": "action_item",
      "summary": "Room 3: bring a stuffed animal for Teddy Bear Day on Friday",
      "action_by": "2026-04-10",
      "urgency": "normal",
      "original_subject": "Weekly Newsletter - Room 3",
      "message_id": "..."
    }
  ]

Requires: google-api-python-client (gmail.readonly scope), openai
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
CACHE_DIR = os.path.join(WORKSPACE, "cache")
SEEN_PATH = os.path.join(CACHE_DIR, "seen-activity-emails.json")

# Activity providers — search terms for Gmail
PROVIDERS = [
    {
        "name": "Example Preschool",
        "query": "from:ExamplePreschool newer_than:2d",
        "context": "Example Preschool is Avery's school. She is in Room 3. Watch for: closures, early dismissals, spirit days, field trips, items to bring/prep/wear, parent events, schedule changes. Room 3-specific items are highest priority.",
    },
    {
        "name": "Example Swim School",
        "query": "from:ExampleSwim newer_than:2d",
        "context": "Example Swim School is Avery's Monday swimming class (4:30-5:00 PM). Watch for: class cancellations, reschedules, pool closures, makeup classes.",
    },
    {
        "name": "Example Ballet Studio",
        "query": "(from:tutu OR from:tutuschool) newer_than:2d",
        "context": "Example Ballet Studio is Avery's Sunday ballet class (9:00 AM). Watch for: class cancellations, recital dates, costume requirements, schedule changes.",
    },
]

LLM_PROMPT = """You are parsing an email from {provider_name} about a child's activity.

Context: {context}
Today's date: {today}

Email subject: {subject}
Email body (first 2000 chars):
{body}

Extract any schedule-relevant items. For each item, output JSON:
{{
  "type": "closure" | "cancellation" | "schedule_change" | "action_item" | "event" | "none",
  "summary": "one-line description of what's happening",
  "action_by": "YYYY-MM-DD if there's a deadline, else null",
  "urgency": "urgent" | "normal" | "fyi",
  "details": "any extra context"
}}

If the email has NO schedule-relevant content (marketing, general newsletters without action items), return:
{{"type": "none"}}

Return a JSON array of items. Be concise. Focus on things the parent needs to act on or know about."""


def parse_args():
    hours = 48
    dry_run = False
    for i, arg in enumerate(sys.argv):
        if arg == "--hours" and i + 1 < len(sys.argv):
            hours = int(sys.argv[i + 1])
        if arg == "--dry-run":
            dry_run = True
    return hours, dry_run


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

    # Simple message
    if payload.get("mimeType", "").startswith("text/plain"):
        import base64
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Multipart
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            import base64
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Fallback: try HTML
    for part in parts:
        if part.get("mimeType") == "text/html":
            import base64
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                # Strip HTML tags (rough)
                import re
                return re.sub(r"<[^>]+>", " ", html)[:2000]

    return ""


def parse_with_llm(provider, subject, body, today, dry_run=False):
    """Use gpt-5.4-nano to extract schedule items from email."""
    if dry_run:
        return [{"type": "none", "summary": "dry run"}]

    prompt = LLM_PROMPT.format(
        provider_name=provider["name"],
        context=provider["context"],
        today=today,
        subject=subject,
        body=body[:2000],
    )

    try:
        import openai
        client = openai.OpenAI()
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        parsed = json.loads(content)

        # Normalize: could be a single object or array
        if isinstance(parsed, dict):
            if "items" in parsed:
                return parsed["items"]
            return [parsed]
        if isinstance(parsed, list):
            return parsed
        return [{"type": "none"}]

    except Exception as e:
        print(f"LLM parse failed: {e}", file=sys.stderr)
        return [{"type": "none", "error": str(e)}]


def main():
    hours, dry_run = parse_args()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
                body = get_email_body(msg)

                if not body.strip():
                    seen.add(msg_id)
                    continue

                items = parse_with_llm(provider, subject, body, today, dry_run)

                for item in items:
                    if item.get("type") == "none":
                        continue
                    item["source"] = provider["name"]
                    item["original_subject"] = subject
                    item["message_id"] = msg_id
                    results.append(item)

                seen.add(msg_id)

        except Exception as e:
            print(f"Provider {provider['name']} failed: {e}", file=sys.stderr)

    save_seen(seen)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
