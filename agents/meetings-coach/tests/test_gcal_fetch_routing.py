"""Murphy's side of the Mouse/Murphy boundary: is_real_meeting must
return True iff the event carries a videoconference link.

Revised 2026-04-18: attendee count no longer promotes in-person items
(e.g. "exploring ballet" with Sam as attendee) into Murphy's queue —
memory project_meeting_event_routing.md.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_gcal_fetch():
    for mod in ("googleapiclient", "googleapiclient.discovery", "google",
                "google.auth", "google.auth.transport", "google.auth.transport.requests",
                "google.oauth2", "google.oauth2.credentials"):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    path = SCRIPTS_DIR / "gcal-fetch.py"
    spec = importlib.util.spec_from_file_location("murphy_gcal_fetch", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_zoom_link_is_meeting():
    mod = _load_gcal_fetch()
    ev = {"summary": "Syncup", "hangoutLink": "https://meet.google.com/x"}
    assert mod.is_real_meeting(ev, {}) is True


def test_in_person_with_attendees_is_not_a_meeting():
    """The exploring-ballet regression: attendees alone used to flip
    is_real_meeting True. Post-2026-04-18 it must not."""
    mod = _load_gcal_fetch()
    ev = {
        "summary": "exploring ballet",
        "attendees": [{"email": "sam@example.com"}],
    }
    assert mod.is_real_meeting(ev, {}) is False


def test_skip_titles_still_filter_out():
    mod = _load_gcal_fetch()
    ev = {"summary": "Focus block", "hangoutLink": "https://meet.google.com/x"}
    cfg = {"meeting_filters": {"skip_titles": ["focus block"]}}
    assert mod.is_real_meeting(ev, cfg) is False


def test_videoconference_via_description_url():
    mod = _load_gcal_fetch()
    ev = {"summary": "External review", "description": "https://zoom.us/j/123"}
    assert mod.is_real_meeting(ev, {}) is True


def test_no_attendees_no_link_not_a_meeting():
    mod = _load_gcal_fetch()
    assert mod.is_real_meeting({"summary": "Reminder"}, {}) is False
