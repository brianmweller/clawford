"""Shared brain calendar index — single source of truth for
"is this calendar item a meeting?" classification.

Introduced 2026-04-18 to replace the per-agent routing that lived in
gcal-fetch.py and reminder-check.py. Each agent used to run its own
has_videoconference_link check, but Mistress Mouse deliberately blanks
every event's description before classifying — which made her blind
to Webex/Zoom/Meet links pasted into the description. The index is
built once per tick on the RAW Google Calendar events by
agents/shared/scripts/calendar-brain-build.py (the successor to the
2026-04-18–2026-04-23 family-calendar/scripts/calendar-index-build.py)
and written to
~/Dropbox/openclaw-backup/status/calendar-index.json. Murphy and Mouse
both read it; whichever agent matches `owner` picks up the event.

The classifier is pure and testable. The fetch lives in the builder
script. The loader degrades open: if the index file is missing or
malformed, callers should fall back to their local classifier rather
than erroring out.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents.shared.meeting_classifier import has_videoconference_link
from agents.shared.recruiter_domains import is_recruiter_domain


def _organizer_email(raw_event: dict) -> str:
    org = raw_event.get("organizer")
    if isinstance(org, str):
        return org
    if isinstance(org, dict):
        return str(org.get("email") or "")
    return ""


def classify_event(
    raw_event: dict,
    *,
    calendar_id: str,
    workflowy_event_ids: set | None = None,
) -> dict:
    """Return a normalized record for a raw Google Calendar event.

    Shape:
      {
        "id": str,
        "summary": str,
        "start": str,        # dateTime or all-day date
        "end": str,
        "all_day": bool,
        "calendar_id": str,
        "has_video_link": bool,
        "in_workflowy": bool,
        "organizer_is_recruiter": bool,
        "is_meeting": bool,
        "owner": "sergeant-murphy" | "mistress-mouse",
      }

    Routing rule (current, after 2026-04-23 phone-interview fix):
    ``is_meeting`` iff ANY of:

      - The event has a videoconference link (physical signal).
      - the operator has linked it in Workflowy (human-in-the-loop override).
      - The organizer's domain is a known ATS / scheduling platform
        (``schedule@interview.adobe.com``, ``no-reply@recruiting.amazon.com``,
        etc.). Covers phone-only or in-person recruiter interviews that
        Murphy owns by product intent but that would otherwise fail
        the video-link test and misroute to Mouse. 2026-04-23: the
        Adobe "Meeting Confirmation - Sam Smith" (phone screen,
        organizer ``schedule@interview.adobe.com``, no video) was
        landing on Mouse's morning brief until this arm was added.

    The Workflowy tag always wins for promotion when present:
    ``in_workflowy`` flags events the operator has deliberately flagged for
    coaching. ``workflowy_event_ids`` is populated from Murphy's
    ``workflowy-links.json`` cache by the builder; pass an empty set
    when the cache is missing.
    """
    if not isinstance(raw_event, dict):
        raw_event = {}

    start_block = raw_event.get("start") or {}
    end_block = raw_event.get("end") or {}
    start = start_block.get("dateTime") or start_block.get("date") or ""
    end = end_block.get("dateTime") or end_block.get("date") or ""
    all_day = bool(start_block.get("date") and not start_block.get("dateTime"))

    has_video = has_videoconference_link(raw_event)
    wf_ids = workflowy_event_ids or set()
    in_workflowy = raw_event.get("id", "") in wf_ids
    organizer_is_recruiter = is_recruiter_domain(_organizer_email(raw_event))
    is_meeting = has_video or in_workflowy or organizer_is_recruiter

    return {
        "id": raw_event.get("id", ""),
        "summary": raw_event.get("summary", "") or "",
        "start": start,
        "end": end,
        "all_day": all_day,
        "calendar_id": calendar_id,
        "has_video_link": has_video,
        "in_workflowy": in_workflowy,
        "organizer_is_recruiter": organizer_is_recruiter,
        "is_meeting": is_meeting,
        "owner": "sergeant-murphy" if is_meeting else "mistress-mouse",
    }


def load_brain_index(path) -> dict[str, dict]:
    """Read the brain index and return the {event_id: record} dict.

    Missing file or malformed JSON → {}. Callers fall back to their
    local classifier in that case so one-time builder outages don't
    break agent routing.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    events = data.get("events")
    if not isinstance(events, dict):
        return {}
    return events


def meeting_event_ids(path) -> set[str]:
    """Return the set of event IDs the index flagged is_meeting=True.

    Hot path for the --skip-meetings filter — O(1) membership test."""
    return {eid for eid, rec in load_brain_index(path).items()
            if isinstance(rec, dict) and rec.get("is_meeting") is True}
