#!/usr/bin/env python3
"""birthday-miner.py — passive birthday ingestion from Google Calendar.

Weekly cron that scans the user's Google Calendar for recurring birthday
events, resolves the attendee name to a person file under
`people/<slug>.md`, and writes the birthday as an identity fact via
`facts.upsert_fact`. Idempotent on `(source_agent, subject, "birthday")`.

Birthdays surface through `/people [name]` — there is no explicit
`/birthday` command; the fact travels with the person.

Usage:
  python3 birthday-miner.py                 # normal pass (this year's events)
  python3 birthday-miner.py --bootstrap     # wide window for first-run seed

SCRIPT_CONTRACT-compliant: always exits 0, prints one JSON line.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared import brain  # noqa: E402
from agents.shared.facts import upsert_fact  # noqa: E402


SOURCE_AGENT = "connector"
ALIASES_REL = "birthday-aliases.json"

# Title prefixes that indicate a to-do / reminder event, NOT a birthday
# date itself. "Get Dad birthday card" is a reminder to buy a card, not
# the day Dad was born. Match case-insensitive at the start of the title.
_TODO_PREFIXES = ("get ", "bring ", "buy ", "order ", "remember ", "need ")

# Relational prefixes we strip before slug lookup. "Aunt Marcia" → "Marcia";
# person files are keyed on the actual first name, not the relational role.
_RELATIONAL_PREFIXES = (
    "aunt ", "uncle ", "mama ", "papa ", "mom ", "dad ",
    "grandma ", "grandpa ", "nana ", "granny ", "gramps ",
)

# "Priya's Birthday", "Priya's B-day", "Mom's bday", "🎂 Priya", "Birthday: Alex"
# Capture one plausible name token before or after the birthday keyword.
_NAME_RE_POSS = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z \-]*?)(?:'s|s')?\s+(?:birthday|b-?day|🎂)\b",
    re.IGNORECASE,
)
_NAME_RE_COLON = re.compile(
    r"(?:birthday|b-?day|🎂)\s*[:\-–]\s*(?P<name>[A-Za-z][A-Za-z \-]*)",
    re.IGNORECASE,
)
_NAME_RE_CAKE_PREFIX = re.compile(
    r"^\s*🎂\s*(?P<name>[A-Za-z][A-Za-z \-]*)",
)


def extract_name_from_title(title: str) -> str | None:
    """Best-effort parse of the birthday-owner name from a GCal event title.

    Returns the raw display name (not slug). Whitespace-trimmed. None if:
      - the title is a to-do reminder ("Get X birthday card"),
      - the title is a generic greeting ("Happy birthday!"),
      - or no plausible name anchors the regex.

    This is a heuristic — a miss is fine; the miner will skip silently
    and the operator can add an explicit entry in birthday-aliases.json.
    """
    if not title:
        return None
    t = title.strip()
    t_lower = t.lower()

    # Filter to-do / reminder events early. These mention "birthday" but
    # the event date is when the operator needs to buy a card, not the B-day.
    for prefix in _TODO_PREFIXES:
        if t_lower.startswith(prefix):
            return None

    # Skip event titles that don't anchor the name before the keyword —
    # "Happy birthday!" / "Birthday Party at X".
    if t_lower.startswith("happy birthday") or t_lower.startswith("birthday party"):
        return None

    # Parties without a person name before "birthday" are ambiguous —
    # if the word "party" appears and the pre-keyword name didn't
    # survive the regex cleanly, skip. We still allow "Violet's Birthday"
    # (without "party") to land.
    if " birthday party" in t_lower:
        return None

    for regex in (_NAME_RE_POSS, _NAME_RE_COLON, _NAME_RE_CAKE_PREFIX):
        m = regex.search(t)
        if m:
            name = m.group("name").strip()
            # Remove the LITERAL "'s" or "s'" suffix if the regex didn't
            # already consume it (rstrip would strip char-class members
            # and eat legitimate trailing letters like the final 's' of
            # "Phyllis" — use removesuffix instead).
            name = name.removesuffix("'s").removesuffix("s'").strip()
            # Reject the literal "Birthday" or bare greeting words
            if name.lower() in {"birthday", "b-day", "bday", "happy"}:
                continue
            return name
    return None


def strip_relational_prefix(name: str | None) -> str | None:
    """'Aunt Marcia' → 'Marcia'. Idempotent; plain names pass through."""
    if not name:
        return name
    stripped = name.strip()
    lower = stripped.lower()
    for prefix in _RELATIONAL_PREFIXES:
        if lower.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def load_aliases_cfg(path: Path) -> dict:
    """Load operator-supplied alias map + manual birthday entries.

    Shape:
        {
          "aliases": {"Mom": "priya-rivera", ...},
          "manual_birthdays": {"slug": "YYYY-MM-DD", ...}
        }

    Missing file returns empty cfg (degrades gracefully to GCal-only).
    """
    default = {"aliases": {}, "manual_birthdays": {}}
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return {
        "aliases": dict(data.get("aliases") or {}),
        "manual_birthdays": dict(data.get("manual_birthdays") or {}),
    }


def _birthday_date_from_event(event: dict) -> str | None:
    """Extract ISO date YYYY-MM-DD from a GCal event's start.

    Prefers the all-day ``start.date`` form (how Google stores birthdays);
    falls back to the date portion of ``start.dateTime`` if someone
    entered a timed event.
    """
    start = event.get("start") or {}
    if "date" in start:
        return start["date"]
    dt = start.get("dateTime")
    if dt:
        # ISO 8601 like 2026-09-15T10:00:00-07:00
        return dt.split("T", 1)[0]
    return None


def _resolve_slug(name: str, aliases_cfg: dict) -> str | None:
    """Map an extracted calendar-event name to a person slug, trying:
      1. aliases dict under the raw extracted name
      2. aliases dict under the relational-prefix-stripped name
      3. brain.get_person(stripped name)
      4. brain.get_person(raw name)
    Returns None if nothing resolves."""
    alias_map = aliases_cfg.get("aliases") or {}
    if name in alias_map:
        return alias_map[name]

    stripped = strip_relational_prefix(name)
    if stripped and stripped != name and stripped in alias_map:
        return alias_map[stripped]

    if stripped and stripped != name:
        person = brain.get_person(stripped)
        if person is not None:
            return person["slug"]

    person = brain.get_person(name)
    if person is not None:
        return person["slug"]
    return None


def process_events(
    events: list[dict],
    *,
    facts_dir: Path,
    aliases_cfg: dict | None = None,
) -> dict:
    """Iterate GCal events, resolve owners to person files, upsert facts.

    Also processes ``manual_birthdays`` from aliases_cfg — operator-
    supplied slug→date entries that bypass GCal entirely (useful for
    birthdays the operator knows but doesn't have on a calendar).

    Returns a summary dict with counters. Scoped so the cron entrypoint
    and the test harness can share the same orchestration.
    """
    aliases_cfg = aliases_cfg or {"aliases": {}, "manual_birthdays": {}}
    scanned = 0
    matched = 0
    updated = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    # Pass 1: Google Calendar events
    for event in events:
        scanned += 1
        title = event.get("summary") or ""
        name = extract_name_from_title(title)
        if not name:
            continue
        slug = _resolve_slug(name, aliases_cfg)
        if slug is None:
            continue
        date_str = _birthday_date_from_event(event)
        if not date_str:
            continue

        matched += 1
        result = upsert_fact(
            facts_dir=facts_dir,
            subject=slug,
            category="identity",
            content=f"Birthday: {date_str}",
            source_agent=SOURCE_AGENT,
            source_type="derived",
            source_detail=f"birthday-miner/calendar/{title}",
            confidence=0.9,
            idempotency_key="birthday",
            recorded_at=now,
        )
        if result["status"] == "created":
            updated += 1
        else:
            skipped += 1

    # Pass 2: manual entries in birthday-aliases.json
    for slug, date_str in (aliases_cfg.get("manual_birthdays") or {}).items():
        if not slug or not date_str:
            continue
        result = upsert_fact(
            facts_dir=facts_dir,
            subject=slug,
            category="identity",
            content=f"Birthday: {date_str}",
            source_agent=SOURCE_AGENT,
            source_type="operator",
            source_detail="birthday-miner/manual",
            confidence=1.0,
            idempotency_key="birthday",
            recorded_at=now,
        )
        if result["status"] == "created":
            updated += 1
        else:
            skipped += 1

    return {
        "scanned": scanned,
        "matched": matched,
        "updated": updated,
        "skipped": skipped,
    }


def _build_gcal_service():
    """Duplicates daily-refresh's pattern — connector's own token first,
    fallback to sibling workspaces. Returns a GCal v3 service."""
    import os

    from google.auth.transport.requests import Request  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    candidates = [
        Path(os.path.expanduser("~/.clawford/connector-workspace/token.json")),
        Path(os.path.expanduser("~/.clawford/family-calendar-workspace/token.json")),
        Path(os.path.expanduser("~/.clawford/meetings-coach-workspace/token.json")),
    ]
    token_path = next((p for p in candidates if p.exists()), None)
    if token_path is None:
        raise RuntimeError("No Google token.json found in any clawford workspace")

    token_data = json.loads(token_path.read_text(encoding="utf-8"))
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_data["token"] = creds.token
        token_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    if not creds.valid:
        raise RuntimeError(f"Google credentials invalid: {token_path}")

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _fetch_birthday_events(service, bootstrap: bool) -> list[dict]:
    """List calendar events matching the birthday keyword family.

    Windowing:
      - Normal pass: today → +365 days. Recurring yearly events expand
        to one occurrence in that window, which is all we need.
      - Bootstrap pass: -30 → +365 days, to catch anything scheduled in
        the last month under the new miner.
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    start_back = 30 if bootstrap else 0
    time_min = (now - timedelta(days=start_back)).isoformat()
    time_max = (now + timedelta(days=365)).isoformat()

    events: list[dict] = []
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId="primary",
                q="birthday",
                singleEvents=True,
                orderBy="startTime",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def run(bootstrap: bool) -> dict:
    import os

    service = _build_gcal_service()
    events = _fetch_birthday_events(service, bootstrap=bootstrap)
    facts_dir = brain.dropbox_brain_root() / "facts"
    aliases_path = Path(
        os.path.expanduser("~/.clawford/connector-workspace") ) / ALIASES_REL
    aliases_cfg = load_aliases_cfg(aliases_path)
    summary = process_events(events, facts_dir=facts_dir, aliases_cfg=aliases_cfg)
    return {
        "status": "ok",
        **summary,
        "bootstrap": bootstrap,
        "aliases_loaded": len(aliases_cfg.get("aliases", {})),
        "manual_loaded": len(aliases_cfg.get("manual_birthdays", {})),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--bootstrap",
        action="store_true",
        help="Wider window for first-run seed (-30d..+365d)",
    )
    args = ap.parse_args()

    try:
        result = run(bootstrap=args.bootstrap)
    except Exception as exc:  # noqa: BLE001
        result = {
            "status": "error",
            "error": str(exc),
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
