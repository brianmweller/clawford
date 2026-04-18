#!/usr/bin/env python3
"""
person-bootstrap.py — Create person files from calendar attendees.

Reads event data (from gcal-fetch.py cache), extracts unique attendees,
and creates person files in the shared brain's /people/ directory for
anyone not already present.

Usage:
  python3 person-bootstrap.py --from-events cache/events-2026-04-08.json
  python3 person-bootstrap.py --from-events cache/events-2026-04-08.json --dry-run

Output JSON:
  {
    "created": ["jane-doe", "bob-martinez"],
    "skipped": ["alice-chen"],
    "errors": []
  }
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

BRAIN_PEOPLE = os.path.expanduser("~/Dropbox/openclaw-backup/people")


def parse_args():
    events_file = None
    dry_run = "--dry-run" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--from-events" and i + 1 < len(sys.argv):
            events_file = sys.argv[i + 1]

    return events_file, dry_run


def email_to_slug(email):
    """Convert email to a person slug: jane.doe@example.com -> jane-doe."""
    local = email.split("@")[0]
    # Replace dots, underscores with hyphens, strip non-alphanumeric
    slug = re.sub(r"[._]+", "-", local)
    slug = re.sub(r"[^a-z0-9-]", "", slug.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def name_to_slug(name):
    """Convert display name to a person slug: Jane Doe -> jane-doe."""
    slug = re.sub(r"[^a-z0-9\s]", "", name.lower())
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug


def person_exists(slug):
    """Check if a person file already exists."""
    path = os.path.join(BRAIN_PEOPLE, f"{slug}.md")
    return os.path.exists(path)


def create_person_file(slug, name, email):
    """Create a person file in the shared brain."""
    path = os.path.join(BRAIN_PEOPLE, f"{slug}.md")

    content = f"""# {name}

- **slug:** {slug}
- **circles:** professional-outer
- **relationship:** professional contact
- **google_contact_id:** —
- **email:** {email}
- **phone:** —
- **platforms:** email
- **preferred_channel:** email
- **last_interaction:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
- **notes:** Auto-created by Sergeant Murphy from calendar attendee data.
"""

    with open(path, "w") as f:
        f.write(content)

    return path


def main():
    events_file, dry_run = parse_args()

    if not events_file:
        print(json.dumps({"status": "error", "message": "Usage: --from-events <file>"}))
        sys.exit(1)

    if not os.path.exists(events_file):
        print(json.dumps({"status": "error", "message": f"File not found: {events_file}"}))
        sys.exit(1)

    # Ensure people directory exists
    os.makedirs(BRAIN_PEOPLE, exist_ok=True)

    with open(events_file) as f:
        data = json.load(f)

    events = data.get("events", [])

    # Collect unique attendees
    seen_emails = set()
    attendees = []
    for event in events:
        if not event.get("is_real_meeting", False):
            continue
        for att in event.get("attendees", []):
            email = att.get("email", "").lower().strip()
            if email and email not in seen_emails:
                seen_emails.add(email)
                attendees.append({
                    "email": email,
                    "name": att.get("name", email.split("@")[0]),
                })

    created = []
    skipped = []
    errors = []

    for att in attendees:
        email = att["email"]
        name = att["name"]

        # Try name-based slug first, fall back to email
        slug = name_to_slug(name) if name else email_to_slug(email)
        if not slug:
            slug = email_to_slug(email)

        if person_exists(slug):
            skipped.append(slug)
            continue

        # Also check email-based slug
        email_slug = email_to_slug(email)
        if email_slug != slug and person_exists(email_slug):
            skipped.append(email_slug)
            continue

        if dry_run:
            created.append(slug)
            continue

        try:
            create_person_file(slug, name, email)
            created.append(slug)
        except Exception as e:
            errors.append({"slug": slug, "error": str(e)})

    result = {
        "status": "ok",
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    import json as _contract_json
    import sys as _contract_sys
    _contract_status = "ok"
    _contract_error = None
    try:
        _contract_rc = main()
        if _contract_rc not in (0, None):
            _contract_status = "error"
            _contract_error = f"main returned {_contract_rc}"
    except SystemExit as _contract_e:
        if _contract_e.code not in (0, None):
            _contract_status = "error"
            _contract_error = f"main exited with code {_contract_e.code}"
    except BaseException as _contract_e:  # noqa: BLE001
        _contract_status = "error"
        _contract_error = str(_contract_e)[:200]
    _contract_envelope = {"status": _contract_status}
    if _contract_error:
        _contract_envelope["error"] = _contract_error
    print(_contract_json.dumps(_contract_envelope))
    _contract_sys.exit(0)
