"""Tests for calendar-index-build.py.

The builder fetches the operator's + Sam's calendars, classifies each raw
event (description intact) with the shared classifier, and writes the
result to ~/Dropbox/openclaw-backup/status/calendar-index.json. These
tests exercise build_index() with a fake Google service so the
classification + merge logic is covered without network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


_GOOGLE_STUBS = (
    "googleapiclient", "googleapiclient.discovery", "google",
    "google.auth", "google.auth.transport", "google.auth.transport.requests",
    "google.oauth2", "google.oauth2.credentials",
)


def _load():
    """Load calendar-index-build.py with temporary google.* stubs.

    Stubs are inserted ONLY for names not already in sys.modules, and
    removed on exit — otherwise they leak into later tests whose probes
    actually exercise the real Google client (test_heartbeat's
    google_auth check in particular)."""
    injected: list[str] = []
    for mod in _GOOGLE_STUBS:
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
            injected.append(mod)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    path = SCRIPTS_DIR / "calendar-index-build.py"
    spec = importlib.util.spec_from_file_location("calendar_index_build", path)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    finally:
        for mod in injected:
            sys.modules.pop(mod, None)
    return m


class _FakeEventsCall:
    def __init__(self, pages):
        self._pages = list(pages)

    def list(self, **_kwargs):
        page_token = _kwargs.get("pageToken")
        if page_token is None:
            self._cursor = 0
        out = self._pages[self._cursor] if self._cursor < len(self._pages) else {"items": []}
        self._cursor += 1
        return _FakeExec(out)


class _FakeExec:
    def __init__(self, payload): self._payload = payload
    def execute(self): return self._payload


class _FakeService:
    def __init__(self, by_calendar: dict[str, list[dict]]):
        self._by_calendar = by_calendar

    def events(self):
        svc = self
        class _Ev:
            def list(_self, calendarId, **_kw):
                payload = {"items": svc._by_calendar.get(calendarId, [])}
                return _FakeExec(payload)
        return _Ev()


def _raw(id_: str, summary: str, **extra) -> dict:
    base = {"id": id_, "summary": summary, "status": "confirmed",
            "start": {"dateTime": "2026-04-18T10:00:00-07:00"},
            "end": {"dateTime": "2026-04-18T10:30:00-07:00"}}
    base.update(extra)
    return base


def test_build_index_classifies_events_across_calendars():
    mod = _load()
    service = _FakeService({
        "operator@gmail.com": [
            _raw("m1", "Sync", hangoutLink="https://meet.google.com/abc"),
            _raw("e1", "Exploring Ballet", description="meet at studio"),
        ],
        "sam@gmail.com": [
            _raw("e2", "Swim Class", description=""),
        ],
    })
    config = {"calendars": [{"id": "operator@gmail.com"}, {"id": "sam@gmail.com"}]}
    now = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    idx = mod.build_index(config, service, now)

    assert idx["event_count"] == 3
    assert idx["events"]["m1"]["is_meeting"] is True
    assert idx["events"]["m1"]["owner"] == "sergeant-murphy"
    assert idx["events"]["e1"]["is_meeting"] is False
    assert idx["events"]["e1"]["owner"] == "mistress-mouse"
    assert idx["events"]["e2"]["is_meeting"] is False


def test_build_index_fairport_case_description_webex_link_classifies_as_meeting():
    """The Fairport regression: an in-person-looking event whose
    description carries a Webex URL must be classified as Murphy's."""
    mod = _load()
    desc = "Do not delete.\nJoin at https://hightower.webex.com/hightower/j.php?MTID=m329"
    service = _FakeService({
        "operator@gmail.com": [_raw("fairport", "Fairport Check In", description=desc)],
    })
    now = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    idx = mod.build_index({"calendars": [{"id": "operator@gmail.com"}]}, service, now)

    rec = idx["events"]["fairport"]
    assert rec["is_meeting"] is True, rec
    assert rec["owner"] == "sergeant-murphy"
    # Description itself must NOT leak into the brain index.
    assert "description" not in rec


def test_build_index_cancelled_events_are_skipped():
    mod = _load()
    service = _FakeService({
        "operator@gmail.com": [
            _raw("a", "Keep", hangoutLink="https://meet.google.com/a"),
            _raw("b", "Cancelled", status="cancelled",
                 hangoutLink="https://meet.google.com/b"),
        ],
    })
    now = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    idx = mod.build_index({"calendars": [{"id": "operator@gmail.com"}]}, service, now)
    assert set(idx["events"].keys()) == {"a"}


def test_build_index_cross_calendar_duplicate_keeps_video_winning_copy():
    """When the same event id appears on two calendars (invite
    propagation) and ONE copy exposes the video link, that copy must
    win the classification so the authoritative read carries."""
    mod = _load()
    # the operator's calendar sees the full Webex link in description.
    brian_copy = _raw("shared", "Fairport Check In",
                      description="https://hightower.webex.com/hightower/j.php?m=1")
    # Sam's calendar (invite propagation) has no description.
    sam_copy = _raw("shared", "Fairport Check In", description="")
    mod_result = mod.build_index(
        {"calendars": [{"id": "sam@gmail.com"}, {"id": "operator@gmail.com"}]},
        _FakeService({"sam@gmail.com": [sam_copy], "operator@gmail.com": [brian_copy]}),
        datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc),
    )
    assert mod_result["events"]["shared"]["is_meeting"] is True


def test_build_index_workflowy_tag_promotes_in_person_event_to_meeting():
    """the operator tagged 'Coffee with Dan' in Workflowy — that's his
    human-in-the-loop override. Even without a video link, the brain
    index must flag it is_meeting=True so Murphy owns it."""
    mod = _load()
    coffee = _raw("coffee-123", "Coffee with Dan",
                  attendees=[{"email": "dan@x.com"}])
    service = _FakeService({"operator@gmail.com": [coffee]})
    idx = mod.build_index(
        {"calendars": [{"id": "operator@gmail.com"}]},
        service,
        datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc),
        workflowy_event_ids={"coffee-123"},
    )
    rec = idx["events"]["coffee-123"]
    assert rec["is_meeting"] is True
    assert rec["in_workflowy"] is True
    assert rec["owner"] == "sergeant-murphy"


def test_build_index_reports_errors_per_calendar():
    mod = _load()

    class _Flaky(_FakeService):
        def events(self):
            svc = self
            class _Ev:
                def list(_self, calendarId, **_kw):
                    if calendarId == "broken@example.com":
                        raise RuntimeError("boom")
                    return _FakeExec({"items": svc._by_calendar.get(calendarId, [])})
            return _Ev()

    service = _Flaky({"ok@example.com": [_raw("a", "Keep", hangoutLink="x")]})
    now = datetime(2026, 4, 18, 17, 0, tzinfo=timezone.utc)
    idx = mod.build_index(
        {"calendars": [{"id": "ok@example.com"}, {"id": "broken@example.com"}]},
        service, now,
    )
    assert idx["event_count"] == 1
    assert any(e["calendar_id"] == "broken@example.com" for e in idx["errors"])
