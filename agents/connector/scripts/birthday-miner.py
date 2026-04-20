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

# "Priya's Birthday", "Priya's B-day", "Mom's bday", "🎂 Priya", "Birthday: Alex"
# Capture one plausible name token before or after the birthday keyword.
_NAME_RE_POSS = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z '\-]*?)(?:'s|s')?\s+(?:birthday|b-?day|🎂)\b",
    re.IGNORECASE,
)
_NAME_RE_COLON = re.compile(
    r"(?:birthday|b-?day|🎂)\s*[:\-–]\s*(?P<name>[A-Za-z][A-Za-z '\-]*)",
    re.IGNORECASE,
)
_NAME_RE_CAKE_PREFIX = re.compile(
    r"^\s*🎂\s*(?P<name>[A-Za-z][A-Za-z '\-]*)",
)


def extract_name_from_title(title: str) -> str | None:
    """Best-effort parse of the birthday-owner name from a GCal event title.

    Returns the raw display name (not slug). Whitespace-trimmed. None if
    no plausible name is found. This is a heuristic — a miss is fine;
    the miner will skip silently and the operator can edit the event title.
    """
    if not title:
        return None
    t = title.strip()
    for regex in (_NAME_RE_POSS, _NAME_RE_COLON, _NAME_RE_CAKE_PREFIX):
        m = regex.search(t)
        if m:
            name = m.group("name").strip().rstrip("'s").strip()
            # Reject the literal "Birthday" as a name
            if name.lower() in {"birthday", "b-day", "bday"}:
                continue
            return name
    return None


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


def process_events(events: list[dict], *, facts_dir: Path) -> dict:
    """Iterate GCal events, resolve owners to person files, upsert facts.

    Returns a summary dict with counters. Scoped so the cron entrypoint
    and the test harness can share the same orchestration.
    """
    scanned = 0
    matched = 0
    updated = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    for event in events:
        scanned += 1
        title = event.get("summary") or ""
        name = extract_name_from_title(title)
        if not name:
            continue
        person = brain.get_person(name)
        if person is None:
            continue
        date_str = _birthday_date_from_event(event)
        if not date_str:
            continue

        matched += 1
        result = upsert_fact(
            facts_dir=facts_dir,
            subject=person["slug"],
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
    service = _build_gcal_service()
    events = _fetch_birthday_events(service, bootstrap=bootstrap)
    facts_dir = brain.dropbox_brain_root() / "facts"
    summary = process_events(events, facts_dir=facts_dir)
    return {"status": "ok", **summary, "bootstrap": bootstrap}


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
