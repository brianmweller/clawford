"""Shared meeting/event classifier for the Sergeant Murphy
(meetings-coach) and Mistress Mouse (family-calendar) routing boundary.

Rule (2026-04-18): a Google Calendar event is a "meeting" (Murphy's
queue) if and only if it carries a videoconferencing link — Google
Meet, Zoom, Microsoft Teams, or Webex. Everything else is an "event"
(Mouse's queue). Supersedes the pre-2026-04-18 Workflowy-link boundary
which couldn't stay consistent when an in-person invite carried
attendees OR when the operator tagged it in Workflowy.

Both agents MUST apply the same predicate or the two queues will
overlap or leak — see memory project_meeting_event_routing.md.
"""
from __future__ import annotations


_VIDEO_URL_PATTERNS = (
    "https://zoom.us/",
    "https://meet.google.com/",
    "https://teams.microsoft.com/",
    "https://teams.live.com/",
    ".webex.com/",
)


def has_videoconference_link(event) -> bool:
    """True iff the event carries a videoconferencing link.

    Checks, in order:
      1. `hangoutLink` — Google Meet auto-attached by Google Calendar.
      2. `conferenceData.entryPoints[].entryPointType == "video"` —
         third-party conferences (Zoom, Teams, Webex) configured via the
         Conferencing API.
      3. URL pasted into `description` — catches Zoom/Meet/Teams/Webex
         links in invites that skipped conferenceData.
    """
    if not isinstance(event, dict):
        return False

    if event.get("hangoutLink"):
        return True

    conf = event.get("conferenceData") or {}
    for entry in conf.get("entryPoints") or []:
        if isinstance(entry, dict) and entry.get("entryPointType") == "video":
            if entry.get("uri"):
                return True

    desc = event.get("description") or ""
    if desc:
        lower = desc.lower()
        for pattern in _VIDEO_URL_PATTERNS:
            if pattern in lower:
                return True

    return False
