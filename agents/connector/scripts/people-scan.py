#!/usr/bin/env python3
"""
people-scan.py — Scan people files and compute check-in status.

Reads all person files from the shared brain, loads circle cadences from
connector-config.json, and classifies each person as overdue, approaching,
or healthy based on their last_interaction date and circle cadence.

Usage:
  python3 people-scan.py                       # All people
  python3 people-scan.py --overdue-only        # Only overdue
  python3 people-scan.py --circle friends-close # Filter by circle
  python3 people-scan.py --person mike-chen    # Single person

Output JSON:
  {
    "overdue": [...],
    "approaching": [...],
    "healthy": [...],
    "summary": {"total": N, "overdue": N, "approaching": N, "skipped": N}
  }
"""

import glob
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone

BRAIN_PEOPLE = os.path.expanduser("~/Dropbox/openclaw-backup/people")
CONFIG_FILE = os.path.expanduser("~/.openclaw/connector-workspace/connector-config.json")
UPCOMING_CACHE = os.path.expanduser("~/.openclaw/connector-workspace/upcoming-meetings.json")

APPROACHING_WINDOW_DAYS = 7  # Flag people within 7 days of their cadence


def parse_args():
    overdue_only = "--overdue-only" in sys.argv
    circle_filter = None
    person_filter = None

    for i, arg in enumerate(sys.argv):
        if arg == "--circle" and i + 1 < len(sys.argv):
            circle_filter = sys.argv[i + 1]
        if arg == "--person" and i + 1 < len(sys.argv):
            person_filter = sys.argv[i + 1]

    return overdue_only, circle_filter, person_filter


def load_config():
    """Load circle cadences from connector-config.json."""
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"Config not found: {CONFIG_FILE}")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _load_upcoming_meeting_emails() -> set:
    """Read upcoming-meetings.json written by daily-refresh.py.

    Returns a lowercased set of email addresses with a confirmed
    calendar meeting in the next LOOKAHEAD_DAYS window. Missing or
    malformed file → empty set (filter is a no-op)."""
    if not os.path.exists(UPCOMING_CACHE):
        return set()
    try:
        with open(UPCOMING_CACHE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    emails = (data or {}).get("emails") or {}
    if not isinstance(emails, dict):
        return set()
    return {str(e).lower() for e in emails.keys() if e and "@" in str(e)}


def parse_person_file(filepath):
    """Parse a person markdown file into a dict of fields."""
    with open(filepath) as f:
        content = f.read()

    person = {}
    for line in content.split("\n"):
        line = line.strip()
        # Accept both "- **key:** value" (current template) and
        # "- **key**: value" (older format used by some agents).
        match = re.match(r"- \*\*(\w[\w_]*)(?::\*\*|\*\*:)\s*(.*)", line)
        if match:
            key = match.group(1).strip()
            value = match.group(2).strip()
            if value == "—" or value == "":
                value = None
            person[key] = value

    # Extract name from heading
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# ") and not line.startswith("# {"):
            person["name"] = line[2:].strip()
            break

    return person


def get_cadence_for_person(person, config):
    """Find the tightest (smallest) nudge-eligible cadence for a person."""
    circles_str = person.get("circles", "")
    if not circles_str:
        return None

    circles = [c.strip() for c in circles_str.split(",")]
    skip_circles = config.get("nudge", {}).get("skip_circles", [])

    best_cadence = None
    best_circle = None

    for circle in circles:
        if circle in skip_circles:
            continue
        cadence_info = config.get("cadences", {}).get(circle, {})
        if not cadence_info.get("nudge", False):
            continue
        check_days = cadence_info.get("check_days")
        if check_days and (best_cadence is None or check_days < best_cadence):
            best_cadence = check_days
            best_circle = circle

    if best_cadence is None:
        return None, None
    return best_cadence, best_circle


def run() -> dict:
    overdue_only, circle_filter, person_filter = parse_args()
    config = load_config()
    upcoming_emails = _load_upcoming_meeting_emails()

    # Find all person files
    pattern = os.path.join(BRAIN_PEOPLE, "*.md")
    files = glob.glob(pattern)

    overdue = []
    approaching = []
    healthy = []
    demoted_upcoming = []
    skipped = 0
    today = datetime.now(timezone.utc).date()

    for filepath in sorted(files):
        basename = os.path.basename(filepath)
        if basename == "_template.md":
            continue

        person = parse_person_file(filepath)
        slug = person.get("slug") or basename.replace(".md", "")

        # Filter by person if specified
        if person_filter and slug != person_filter:
            continue

        # Filter by circle if specified
        circles_str = person.get("circles", "")
        circles = [c.strip() for c in circles_str.split(",") if c.strip()]

        if circle_filter and circle_filter not in circles:
            continue

        # Check if all circles are skipped
        skip_circles = config.get("nudge", {}).get("skip_circles", [])
        non_skipped = [c for c in circles if c not in skip_circles]
        if not non_skipped:
            skipped += 1
            continue

        # Get cadence
        cadence_result = get_cadence_for_person(person, config)
        cadence_days, cadence_circle = cadence_result if cadence_result != (None, None) else (None, None)

        if cadence_days is None:
            skipped += 1
            continue

        # Parse last_interaction
        last_interaction_str = person.get("last_interaction")
        if not last_interaction_str:
            # No interaction recorded — treat as very overdue
            days_since = 999
        else:
            try:
                last_date = datetime.strptime(last_interaction_str, "%Y-%m-%d").date()
                days_since = (today - last_date).days
            except ValueError:
                days_since = 999

        days_overdue = days_since - cadence_days

        entry = {
            "slug": slug,
            "name": person.get("name", slug),
            "circles": circles,
            "relationship": person.get("relationship", ""),
            "relationship_type": person.get("relationship_type", ""),
            "preferred_channel": person.get("preferred_channel", ""),
            "tone": person.get("tone", ""),
            "context_notes": person.get("context_notes", ""),
            "last_interaction": last_interaction_str,
            "days_since": days_since,
            "cadence_days": cadence_days,
            "cadence_circle": cadence_circle,
            "days_overdue": days_overdue,
        }

        # Component C: a confirmed upcoming meeting demotes the
        # person out of overdue/approaching. Healthy people are
        # unaffected — the filter only prevents false nudges.
        # Checks both the primary `email` and any `alt_emails` listed.
        person_emails = set()
        primary_email = (person.get("email") or "").lower().strip()
        if primary_email and "@" in primary_email:
            person_emails.add(primary_email)
        alt = person.get("alt_emails") or ""
        for part in alt.split(","):
            addr = part.strip().lower()
            if "@" in addr:
                person_emails.add(addr)
        has_upcoming = bool(person_emails & upcoming_emails)

        if days_overdue > 0:
            if has_upcoming:
                demoted_upcoming.append(entry)
            else:
                overdue.append(entry)
        elif days_overdue > -APPROACHING_WINDOW_DAYS:
            if has_upcoming:
                demoted_upcoming.append(entry)
            else:
                approaching.append(entry)
        else:
            if not overdue_only:
                healthy.append(entry)

    # Sort overdue by days_overdue descending
    overdue.sort(key=lambda p: -p["days_overdue"])
    # Sort approaching by days until due
    approaching.sort(key=lambda p: p["days_overdue"])

    # Apply max_per_day limit to overdue nudges
    max_per_day = config.get("nudge", {}).get("max_per_day", 5)
    overdue_display = overdue[:max_per_day]

    return {
        "status": "ok",
        "overdue": overdue_display,
        "overdue_total": len(overdue),
        "approaching": approaching,
        "healthy": healthy if not overdue_only else [],
        "demoted_upcoming": demoted_upcoming,
        "summary": {
            "total": len(overdue) + len(approaching) + len(healthy) + len(demoted_upcoming),
            "overdue": len(overdue),
            "approaching": len(approaching),
            "healthy": len(healthy),
            "demoted_upcoming": len(demoted_upcoming),
            "skipped": skipped,
        },
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
