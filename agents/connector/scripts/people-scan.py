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
CONFIG_FILE = os.path.expanduser("~/.clawford/connector-workspace/connector-config.json")
UPCOMING_CACHE = os.path.expanduser("~/.clawford/connector-workspace/upcoming-meetings.json")
SNOOZES_FILE = os.path.expanduser("~/.clawford/connector-workspace/snoozes.json")

APPROACHING_WINDOW_DAYS = 7  # Flag people within 7 days of their cadence

# Map each brain circle to the display group it renders under in the
# morning nudge. Circles not listed here have no display_group (they
# still appear in the flat `overdue` list but don't bucket into a
# grouped section — effectively hiding them from the new multi-
# message delivery format).
CIRCLE_DISPLAY_GROUPS = {
    "family-extended": "family",
    "friends-close": "friends",
    "friends-acquaintance": "friends",
    "professional-inner": "colleagues",
    "professional-outer": "colleagues",
}

# Order groups render in the digest.
DISPLAY_GROUP_ORDER = ("family", "friends", "colleagues")

# Max overdue entries shown per display group.
DEFAULT_MAX_PER_GROUP = 5


def _resolve_display_group(circles: list[str]) -> str | None:
    """Return the first matching display group for a person's circles,
    in the order they appear in the person file. Unmapped circles
    are ignored; returns None if nothing matches."""
    for c in circles:
        g = CIRCLE_DISPLAY_GROUPS.get(c.strip())
        if g:
            return g
    return None


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


def _load_snoozes_raw() -> dict:
    """Read snoozes.json as a raw dict. Missing or malformed → {}."""
    if not os.path.exists(SNOOZES_FILE):
        return {}
    try:
        with open(SNOOZES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_snoozes(data: dict) -> None:
    """Atomic write of the snooze store."""
    os.makedirs(os.path.dirname(SNOOZES_FILE), exist_ok=True)
    tmp = SNOOZES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SNOOZES_FILE)


def _apply_auto_snooze(today, auto_snooze_days: int = 3) -> None:
    """If a last-shown-<yesterday>.json file exists, auto-snooze any
    slug in it that doesn't already have an entry in snoozes.json.

    Rationale: the morning nudge shouldn't repeat yesterday's entries
    when the operator silent-ignores them — pressing no button should mean
    'not now, hide for a few days'. 2026-04-19: shortened from 14d to
    3d because 14 days exceeded the professional-inner cadence (7d),
    which meant a single silent ignore guaranteed a contact would
    never re-surface. 3d is short enough to let a stale colleague
    come back later the same week, long enough to avoid same-day
    re-pings.
    """
    from datetime import timedelta as _td
    yesterday = (today - _td(days=1)).isoformat()
    workspace_dir = os.path.dirname(SNOOZES_FILE) or "."
    last_shown_path = os.path.join(workspace_dir, f"last-shown-{yesterday}.json")
    if not os.path.exists(last_shown_path):
        return
    try:
        with open(last_shown_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return

    slugs = payload.get("slugs") if isinstance(payload, dict) else None
    if not isinstance(slugs, list):
        return

    snoozes = _load_snoozes_raw()
    changed = False
    until = (today + _td(days=auto_snooze_days)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    for slug in slugs:
        if not isinstance(slug, str) or not slug:
            continue
        if slug in snoozes:
            # the operator took action on this one yesterday — preserve it.
            continue
        snoozes[slug] = {
            "status": "auto_snoozed",
            "until": until,
            "set_at": now_iso,
        }
        changed = True

    if changed:
        _write_snoozes(snoozes)


def _load_active_snoozes(today):
    """Return the set of slugs whose until-date is >= today (i.e.
    still actively snoozed)."""
    data = _load_snoozes_raw()
    active = set()
    for slug, entry in data.items():
        if not isinstance(entry, dict):
            continue
        until_str = entry.get("until")
        if not until_str:
            continue
        try:
            until_date = datetime.strptime(until_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if until_date >= today:
            active.add(slug)
    return active


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
    with open(filepath, encoding="utf-8") as f:
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
    # Roll yesterday's unactioned nudges into an auto-snooze so they
    # don't re-appear today. Must run BEFORE _load_active_snoozes so
    # the freshly written entries are in the filter set.
    _apply_auto_snooze(today)
    active_snoozes = _load_active_snoozes(today)

    for filepath in sorted(files):
        basename = os.path.basename(filepath)
        if basename == "_template.md":
            continue

        person = parse_person_file(filepath)
        slug = person.get("slug") or basename.replace(".md", "")

        # Filter by person if specified
        if person_filter and slug != person_filter:
            continue

        # Auto-created stubs (triage_recruiter, sent_recipient,
        # thread_continuity) stay quiet in relationship nudges until
        # the operator elevates them — usually by removing the auto_created
        # field once he wants the contact in the check-in rotation.
        # The triage pipeline still uses these stubs as recognition for
        # email_to_slug; they're invisible only to nudges.
        if person.get("auto_created"):
            skipped += 1
            continue

        # Snooze filter — hide people with active snooze/done/ignore.
        # the operator's nudge buttons write into SNOOZES_FILE; expired
        # entries automatically resurface the person in the next scan.
        if slug in active_snoozes:
            skipped += 1
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

        # Person-ness filter: require at least one reachable contact
        # method. Mining-pipeline entries for WhatsApp chat IDs (e.g.
        # "Am147") and phone-number slugs (e.g. "1-818-3326560") have
        # both email and phone as em-dash (parsed to None); they are
        # not real people and must not show up in nudges.
        if not person.get("email") and not person.get("phone"):
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
            # Raw contact values so morning-relationship-nudge can
            # render clickable mailto:/tel: Telegram anchors instead
            # of the plain channel label. Parsed from the person
            # markdown above; guarded by the reachable-contact filter
            # two blocks up (line 325) so at least one is populated.
            "email": person.get("email", "") or "",
            "phone": person.get("phone", "") or "",
            "tone": person.get("tone", ""),
            "context_notes": person.get("context_notes", ""),
            "last_interaction": last_interaction_str,
            "days_since": days_since,
            "cadence_days": cadence_days,
            "cadence_circle": cadence_circle,
            "days_overdue": days_overdue,
            "display_group": _resolve_display_group(circles),
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

    # Group overdue by display group (family/friends/colleagues),
    # capping each group's shown entries. Only entries with a mapped
    # display_group appear in the grouped view — ungrouped circles
    # still exist in the flat `overdue` list for back-compat.
    max_per_group = config.get("nudge", {}).get("max_per_group", DEFAULT_MAX_PER_GROUP)
    overdue_by_group: dict[str, list[dict]] = {g: [] for g in DISPLAY_GROUP_ORDER}
    for entry in overdue:
        g = entry.get("display_group")
        if g and g in overdue_by_group:
            if len(overdue_by_group[g]) < max_per_group:
                overdue_by_group[g].append(entry)

    return {
        "status": "ok",
        "overdue": overdue_display,
        "overdue_total": len(overdue),
        "overdue_by_group": overdue_by_group,
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
