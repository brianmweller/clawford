"""Tests for agents/meetings-coach/scripts/workflowy-sync.py date-path
navigation + meeting-node creation.

Covers the three bugs that surfaced during the Reddit-recruiter test
case (2026-04-21):

1. ``find_or_create_date_path`` scanned ALL year nodes and picked the
   first ``2026`` it found, which landed under the wrong top-level
   workspace (Notes) on one run. Fix: scope year search to a
   caller-provided ``root_id``.
2. ``create_node`` defaults to prepending new children, so
   ``create_meeting_node`` ended up with Post-meeting ABOVE Pre/during
   and Notes ABOVE Agenda (reverse of the template). Fix:
   ``position="bottom"`` on sub-structure creation.
3. Date computation used naive / UTC datetimes, which on a PT evening
   landed meetings on the wrong weekday node. Fix: ``tz`` kwarg
   converts the meeting_date to operator-local before extracting
   year / month / day.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "workflowy-sync.py"
)
_spec = importlib.util.spec_from_file_location("workflowy_sync", _SCRIPT)
wf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wf)


# ---------------------------------------------------------------------------
# Fixture nodes — a toy Workflowy export with TWO top-level roots
# (Meeting + Notes), each carrying its own 2026 > April 2026 subtree.
# ---------------------------------------------------------------------------


def _nodes():
    """Export fixture:
      Meeting (top-level)
        2026 (meeting_2026)
          April 2026 (meeting_april)
            Wed, Apr 22, 2026 (meeting_apr22)
              existing-meeting (m_existing)
      Notes (top-level)                   ← unrelated workspace
        2026 (notes_2026)
          April 2026 (notes_april)
            Wed, Apr 22, 2026 (notes_apr22)
    """
    return [
        {"id": "meeting_root", "parent_id": None, "name": "Meeting"},
        {"id": "meeting_2026", "parent_id": "meeting_root", "name": "2026"},
        {"id": "meeting_april", "parent_id": "meeting_2026", "name": "April 2026"},
        {"id": "meeting_apr22", "parent_id": "meeting_april",
         "name": "Wed, Apr 22, 2026"},
        {"id": "m_existing", "parent_id": "meeting_apr22",
         "name": "Prior meeting"},
        {"id": "notes_root", "parent_id": None, "name": "Notes"},
        {"id": "notes_2026", "parent_id": "notes_root", "name": "2026"},
        {"id": "notes_april", "parent_id": "notes_2026", "name": "April 2026"},
        {"id": "notes_apr22", "parent_id": "notes_april",
         "name": "Wed, Apr 22, 2026"},
    ]


class _FakeAPI:
    """Capture every create_node call so tests can assert on position
    and parent_id without hitting the real API."""

    def __init__(self):
        self.calls: list[dict] = []
        self._next_id = 1000

    def create_node(self, parent_id, name, position=None, api_key=None):
        self.calls.append(
            {"parent_id": parent_id, "name": name, "position": position}
        )
        nid = f"new_{self._next_id}"
        self._next_id += 1
        return {"item_id": nid}


# ---------------------------------------------------------------------------
# find_meeting_root_id
# ---------------------------------------------------------------------------


def test_find_meeting_root_id_returns_top_level_named_meeting():
    assert wf.find_meeting_root_id(_nodes(), "Meeting") == "meeting_root"


def test_find_meeting_root_id_not_found_returns_none():
    assert wf.find_meeting_root_id(_nodes(), "Inbox") is None


def test_find_meeting_root_id_default_root_name_is_meeting():
    """Default root_name should be 'Meeting' — the convention name
    the operator's Workflowy uses."""
    assert wf.find_meeting_root_id(_nodes()) == "meeting_root"


# ---------------------------------------------------------------------------
# find_or_create_date_path — root_id scoping
# ---------------------------------------------------------------------------


def test_date_path_under_meeting_root_uses_meeting_year(monkeypatch):
    """When root_id is given, the year search is scoped to that root's
    descendants — must NOT return the Notes > 2026 path."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)

    date_id = wf.find_or_create_date_path(
        datetime(2026, 4, 22), _nodes(),
        root_id="meeting_root",
    )
    # Already exists under meeting_root path — should return existing id,
    # NOT create anything new.
    assert date_id == "meeting_apr22"
    assert fake.calls == []


def test_date_path_creates_missing_day_under_correct_root(monkeypatch):
    """A day that doesn't exist yet must be created under the Meeting
    root's April 2026, not under Notes."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)

    date_id = wf.find_or_create_date_path(
        datetime(2026, 4, 25), _nodes(),
        root_id="meeting_root",
    )
    assert date_id.startswith("new_")
    # Exactly one create_node call, parent must be meeting_april.
    assert len(fake.calls) == 1
    assert fake.calls[0]["parent_id"] == "meeting_april"
    assert "Apr 25, 2026" in fake.calls[0]["name"]


def test_date_path_without_root_id_preserves_legacy_behavior(monkeypatch):
    """Backward compat: when root_id is None, the legacy 'first 2026
    year node' path still runs (so existing callers don't regress
    until they opt in)."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)

    date_id = wf.find_or_create_date_path(
        datetime(2026, 4, 22), _nodes(),
    )
    # Either meeting_apr22 or notes_apr22 is acceptable — just that it
    # doesn't crash and returns a valid date-node id.
    assert date_id in ("meeting_apr22", "notes_apr22")


# ---------------------------------------------------------------------------
# find_or_create_date_path — tz conversion
# ---------------------------------------------------------------------------


def test_date_path_converts_utc_datetime_to_pt_date(monkeypatch):
    """A UTC-noon datetime on 2026-04-22T02:00:00Z is Tue, Apr 21 in PT.
    With tz='America/Los_Angeles', the path must resolve to Apr 21,
    not Apr 22."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)

    utc_dt = datetime(2026, 4, 22, 2, 0, tzinfo=timezone.utc)
    wf.find_or_create_date_path(
        utc_dt, _nodes(),
        root_id="meeting_root",
        tz="America/Los_Angeles",
    )
    # Tue Apr 21 doesn't exist under meeting_april; one day-create
    # should fire naming the PT date.
    assert len(fake.calls) == 1
    name = fake.calls[0]["name"]
    assert "Apr 21, 2026" in name
    assert "Tue" in name


def test_date_path_pt_aware_datetime_passes_through(monkeypatch):
    """A PT-aware datetime for 2026-04-22T10:00 PT should resolve to
    Wed Apr 22 regardless of whether tz is passed (the datetime already
    carries PT offset)."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)

    pt_dt = datetime(2026, 4, 22, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    date_id = wf.find_or_create_date_path(
        pt_dt, _nodes(),
        root_id="meeting_root",
        tz="America/Los_Angeles",
    )
    assert date_id == "meeting_apr22"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# create_meeting_node — sub-structure uses position="bottom" so the
# display order matches the template.
# ---------------------------------------------------------------------------


def test_create_meeting_node_uses_position_bottom_for_substructure(monkeypatch):
    """Pre/during → Post-meeting must render top-to-bottom. Since
    Workflowy default is prepend, every sub-structure create_node must
    pass position='bottom' explicitly."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)
    monkeypatch.setattr(wf, "get_export", lambda api_key=None: _nodes())
    monkeypatch.setattr(wf, "get_children", lambda pid, api_key=None: [])

    wf.create_meeting_node(
        meeting_date=datetime(2026, 4, 22, 10, 0,
                              tzinfo=ZoneInfo("America/Los_Angeles")),
        title="Michelle Leist",
        hashtags=["JobSearch", "Reddit"],
        attendees=[{"email": "michelle@rivierapartners.com", "name": "Michelle"}],
        api_key="k",
        root_id="meeting_root",
        tz="America/Los_Angeles",
    )

    # Find the sub-structure calls: Pre/during, Agenda, Notes,
    # Post-meeting, Takeaways all must be position='bottom'.
    template_labels = {"Pre / during", "Post-meeting"}
    agenda_notes_takeaway_markers = ("Agenda", "Notes", "Takeaways")

    substructure_calls = [
        c for c in fake.calls
        if c["name"] in template_labels
        or any(m in c["name"] for m in agenda_notes_takeaway_markers)
    ]
    assert substructure_calls, "expected sub-structure creates"
    for c in substructure_calls:
        assert c["position"] == "bottom", (
            f"sub-structure node {c['name']!r} needs position='bottom' "
            "so the template renders top-down"
        )


def test_create_meeting_node_title_includes_hashtags(monkeypatch):
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)
    monkeypatch.setattr(wf, "get_export", lambda api_key=None: _nodes())
    monkeypatch.setattr(wf, "get_children", lambda pid, api_key=None: [])

    wf.create_meeting_node(
        meeting_date=datetime(2026, 4, 22, 10, 0,
                              tzinfo=ZoneInfo("America/Los_Angeles")),
        title="Michelle Leist",
        hashtags=["JobSearch", "Reddit"],
        attendees=[],
        api_key="k",
        root_id="meeting_root",
        tz="America/Los_Angeles",
    )
    # First create should be the meeting node itself under meeting_apr22.
    meeting_creates = [
        c for c in fake.calls if c["parent_id"] == "meeting_apr22"
    ]
    assert meeting_creates
    assert "Michelle Leist" in meeting_creates[0]["name"]
    assert "#JobSearch" in meeting_creates[0]["name"]
    assert "#Reddit" in meeting_creates[0]["name"]
