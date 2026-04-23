"""agents/shared/calendar_fetch.py — single source of truth for
Google Calendar API fetching + event normalization.

Replaces the two duplicated gcal-fetch.py scripts (Murphy's in
agents/meetings-coach/scripts/, Mouse's in agents/family-calendar/
scripts/). Both agents route through here so:

  - The API interaction is written once.
  - The normalized record shape is a SUPERSET of what either legacy
    script produced — Murphy consumers get attendees + description,
    Mouse consumers filter/blank at read time.
  - The classifier (meeting vs event) is applied consistently via the
    shared ``agents.shared.calendar_index.classify_event`` path.
  - Incremental sync via ``syncToken`` is supported (the legacy
    scripts didn't use it; the brain listener will).

Nothing in here touches disk. Callers wrap it with cache-write logic
(the brain listener, the daily rebuild script).

Key design decisions:

  - ``normalize_event`` is a pure function. Tests exercise it without
    hitting the Google API.
  - ``fetch_events_full`` and ``fetch_events_incremental`` take a
    pre-built ``service`` object (googleapiclient Calendar v3). They
    don't load tokens themselves — that's the caller's job — so test
    harnesses can pass stub services without faking OAuth.
  - ``is_real_meeting`` = ``is_meeting`` AND NOT ``skip_titles`` match.
    The brain stores this as a Murphy-facing flag; Mouse filters on
    ``owner == "mistress-mouse"`` instead.
"""
from __future__ import annotations

from typing import Any

from agents.shared.calendar_index import classify_event
from agents.shared.meeting_classifier import has_videoconference_link


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _extract_conference_link(raw_event: dict) -> str:
    """Extract the video-meeting URL from a raw Google Calendar event.

    Precedence: hangoutLink (canonical Meet) > conferenceData entryPoint
    of type "video" > description URL scan (Zoom / Meet / Teams). The
    last branch catches the cold-recruiter pattern where the invite is
    self-booked and the link lives only in the description body."""
    link = raw_event.get("hangoutLink", "") or ""
    if link:
        return link

    conf = raw_event.get("conferenceData") or {}
    for entry_point in conf.get("entryPoints") or []:
        if entry_point.get("entryPointType") == "video":
            return entry_point.get("uri", "") or ""

    desc = raw_event.get("description") or ""
    if desc:
        for pattern in (
            "https://zoom.us/",
            "https://meet.google.com/",
            "https://teams.microsoft.com/",
        ):
            idx = desc.find(pattern)
            if idx < 0:
                continue
            end = len(desc)
            for ch in (" ", "\n", "\r", '"', "'"):
                pos = desc.find(ch, idx)
                if pos >= 0:
                    end = min(end, pos)
            return desc[idx:end]

    return ""


def _normalize_attendees(raw_event: dict) -> list[dict]:
    """Drop self=True entries (operator's own address) and normalize
    to {email, name, response_status} — the shape the legacy Murphy
    fetcher produced."""
    out: list[dict] = []
    for att in raw_event.get("attendees") or []:
        if not isinstance(att, dict):
            continue
        if att.get("self") is True:
            continue
        email = att.get("email", "") or ""
        name = att.get("displayName") or email.split("@")[0]
        out.append({
            "email": email,
            "name": name,
            "response_status": att.get("responseStatus", "needsAction"),
        })
    return out


def _normalize_organizer(raw_event: dict) -> str:
    """Return the organizer email as a string. Google's shape is
    usually {email, displayName, self}; legacy call sites sometimes
    flatten to a bare string. Handle both."""
    org = raw_event.get("organizer")
    if isinstance(org, str):
        return org
    if isinstance(org, dict):
        return str(org.get("email") or "")
    return ""


def _title_hits_skip_list(summary: str, skip_titles: list[str] | None) -> bool:
    if not skip_titles:
        return False
    s = (summary or "").lower()
    return any((skip or "").lower() in s for skip in skip_titles if skip)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_event(
    raw_event: dict,
    *,
    calendar_id: str,
    calendar_label: str = "",
    calendar_emoji: str = "",
    workflowy_event_ids: set[str] | None = None,
    skip_titles: list[str] | None = None,
) -> dict:
    """Return the canonical brain record for a raw Google Calendar event.

    The record carries everything either agent needs: attendees and
    description for Murphy's prep path, the classification flags for
    Mouse/Murphy routing, skip-title suppression for Murphy's display.
    Consumers never mutate the record; they filter.

    Shape::

        {
          "id": str,
          "calendar_id": str,
          "calendar_label": str,
          "calendar_emoji": str,

          "summary": str,
          "start": str,   # dateTime or all-day date
          "end": str,
          "all_day": bool,
          "location": str,
          "description": str,   # full body, readers blank for display
          "status": str,        # confirmed | cancelled | tentative

          "attendees": [{email, name, response_status}],
          "organizer": str,     # flattened to email string

          "conference_link": str,

          "has_video_link": bool,
          "in_workflowy": bool,
          "is_meeting": bool,   # video OR workflowy (routing predicate)
          "owner": "sergeant-murphy" | "mistress-mouse",

          # Murphy's display filter: is_meeting AND NOT skip_titles match
          "is_real_meeting": bool,
        }
    """
    raw_event = raw_event if isinstance(raw_event, dict) else {}

    classification = classify_event(
        raw_event,
        calendar_id=calendar_id,
        workflowy_event_ids=workflowy_event_ids,
    )

    summary = raw_event.get("summary", "") or ""
    is_skip = _title_hits_skip_list(summary, skip_titles)

    return {
        "id": classification["id"],
        "calendar_id": calendar_id,
        "calendar_label": calendar_label or "",
        "calendar_emoji": calendar_emoji or "",

        "summary": summary,
        "start": classification["start"],
        "end": classification["end"],
        "all_day": classification["all_day"],
        "location": raw_event.get("location", "") or "",
        "description": raw_event.get("description", "") or "",
        "status": raw_event.get("status", "confirmed") or "confirmed",

        "attendees": _normalize_attendees(raw_event),
        "organizer": _normalize_organizer(raw_event),

        "conference_link": _extract_conference_link(raw_event),

        "has_video_link": classification["has_video_link"],
        "in_workflowy": classification["in_workflowy"],
        "organizer_is_recruiter": classification.get(
            "organizer_is_recruiter", False
        ),
        "is_meeting": classification["is_meeting"],
        "owner": classification["owner"],

        "is_real_meeting": (
            classification["is_meeting"] and not is_skip
        ),
    }


# ---------------------------------------------------------------------------
# Service wrappers
# ---------------------------------------------------------------------------


def fetch_events_full(
    service: Any,
    calendar_id: str,
    *,
    time_min: str,
    time_max: str,
    max_per_page: int = 250,
) -> tuple[list[dict], str | None]:
    """Full fetch of events in ``[time_min, time_max)``, paginated.

    Returns ``(events, next_sync_token)``. The sync token on the final
    page is the seed for subsequent ``fetch_events_incremental`` calls.
    Callers persist it.

    ``time_min`` / ``time_max`` are RFC3339 strings with timezone
    offsets (e.g. ``2026-04-23T00:00:00-07:00``). singleEvents=True and
    orderBy=startTime match the legacy scripts' behavior."""
    events: list[dict] = []
    page_token: str | None = None
    next_sync_token: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max_per_page,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.events().list(**kwargs).execute()

        for item in resp.get("items") or []:
            events.append(item)

        # nextSyncToken appears ONLY on the last page of a full list.
        # nextPageToken means more pages to go; both can't coexist.
        next_sync_token = resp.get("nextSyncToken") or next_sync_token
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return events, next_sync_token


def fetch_events_incremental(
    service: Any,
    calendar_id: str,
    sync_token: str,
    *,
    max_per_page: int = 250,
) -> tuple[list[dict] | None, str | None]:
    """Incremental fetch using a stored ``syncToken``. Returns the delta
    (created/changed/cancelled events since last sync) and the new sync
    token.

    On 410 GONE — Google's signal that the sync token has aged out —
    returns ``(None, None)`` so the caller can recover by falling back
    to a bounded full fetch and reseeding."""
    events: list[dict] = []
    page_token: str | None = None
    next_sync_token: str | None = None

    try:
        while True:
            kwargs: dict[str, Any] = {
                "calendarId": calendar_id,
                "syncToken": sync_token,
                "singleEvents": True,
                "maxResults": max_per_page,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.events().list(**kwargs).execute()

            for item in resp.get("items") or []:
                events.append(item)

            next_sync_token = resp.get("nextSyncToken") or next_sync_token
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:  # noqa: BLE001 — broad by design
        # googleapiclient raises HttpError; detect 410 GONE without
        # importing the error class at module load (keeps tests that
        # don't have the SDK installed working).
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 410:
            return None, None
        raise

    return events, next_sync_token
