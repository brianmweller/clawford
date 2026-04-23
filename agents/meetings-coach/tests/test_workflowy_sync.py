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
      Notes (top-level)                  ← real meeting workspace
        2026 (notes_2026)
          April 2026 (notes_april)
            Wed, Apr 22, 2026 (notes_apr22)
              existing-meeting (m_existing)
      OtherRoot (top-level)              ← unrelated top-level workspace
        2026 (other_2026)
          April 2026 (other_april)
            Wed, Apr 22, 2026 (other_apr22)
      Notes > 2024 > ... > 📝 Notes > Meeting (depth 7)   ← stale
        fragment with its own 2026 subtree. find_meeting_root_id
        MUST NOT resolve to this — the 2026-04-22 bug.
    """
    return [
        # Real meeting workspace
        {"id": "notes_root", "parent_id": None, "name": "Notes"},
        {"id": "notes_2026", "parent_id": "notes_root", "name": "2026"},
        {"id": "notes_april", "parent_id": "notes_2026", "name": "April 2026"},
        {"id": "notes_apr22", "parent_id": "notes_april",
         "name": "Wed, Apr 22, 2026"},
        {"id": "m_existing", "parent_id": "notes_apr22",
         "name": "Prior meeting"},
        # Unrelated top-level workspace with its own 2026 subtree
        {"id": "other_root", "parent_id": None, "name": "OtherRoot"},
        {"id": "other_2026", "parent_id": "other_root", "name": "2026"},
        {"id": "other_april", "parent_id": "other_2026", "name": "April 2026"},
        {"id": "other_apr22", "parent_id": "other_april",
         "name": "Wed, Apr 22, 2026"},
        # Stale "Meeting" fragment deep inside an old meeting's notes —
        # the trap that caught Phase 2's first push.
        {"id": "notes_2024", "parent_id": "notes_root", "name": "2024"},
        {"id": "notes_jul24", "parent_id": "notes_2024", "name": "July 2024"},
        {"id": "notes_jul31", "parent_id": "notes_jul24",
         "name": "Wed, Jul 31, 2024"},
        {"id": "old_meeting", "parent_id": "notes_jul31",
         "name": "Old meeting #Tag"},
        {"id": "old_preduring", "parent_id": "old_meeting", "name": "Pre / during"},
        {"id": "old_notes", "parent_id": "old_preduring", "name": "📝  Notes"},
        {"id": "stale_meeting", "parent_id": "old_notes", "name": "Meeting"},
        {"id": "stale_2026", "parent_id": "stale_meeting", "name": "2026"},
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


def test_find_meeting_root_id_returns_top_level_named_notes():
    """the operator's real meeting workspace root is top-level 'Notes'."""
    assert wf.find_meeting_root_id(_nodes(), "Notes") == "notes_root"


def test_find_meeting_root_id_not_found_returns_none():
    """Fail loud when root_name doesn't match ANY top-level node —
    don't silently fall back to deeper matches."""
    assert wf.find_meeting_root_id(_nodes(), "NonExistentRoot") is None


def test_find_meeting_root_id_ignores_stale_deep_nested_match():
    """REGRESSION: a stray 'Meeting' text fragment at depth 7 inside an
    old meeting's 📝 Notes section must NOT be treated as a root. The
    2026-04-22 Reddit-recruiter test landed its whole meeting there
    because the prior fallback resolved to this decoy."""
    # No top-level 'Meeting' exists in the fixture; only a deep-nested
    # 'Meeting' fragment. Must return None, not stale_meeting.
    assert wf.find_meeting_root_id(_nodes(), "Meeting") is None


def test_find_meeting_root_id_default_root_name_is_notes():
    """Default root_name should be 'Notes' — the operator's real top-level
    meeting archive, where production meetings (Sophia, Mohit) live."""
    assert wf.find_meeting_root_id(_nodes()) == "notes_root"


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
        root_id="notes_root",
    )
    # Already exists under meeting_root path — should return existing id,
    # NOT create anything new.
    assert date_id == "notes_apr22"
    assert fake.calls == []


def test_date_path_creates_missing_day_under_correct_root(monkeypatch):
    """A day that doesn't exist yet must be created under the Meeting
    root's April 2026, not under Notes."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)

    date_id = wf.find_or_create_date_path(
        datetime(2026, 4, 25), _nodes(),
        root_id="notes_root",
    )
    assert date_id.startswith("new_")
    # Exactly one create_node call, parent must be meeting_april.
    assert len(fake.calls) == 1
    assert fake.calls[0]["parent_id"] == "notes_april"
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
        root_id="notes_root",
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
        root_id="notes_root",
        tz="America/Los_Angeles",
    )
    assert date_id == "notes_apr22"
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
        root_id="notes_root",
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


def test_push_prep_sections_creates_each_labeled_section(monkeypatch):
    """A fresh Agenda gets each labeled section with its nested items."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)
    monkeypatch.setattr(wf, "get_children", lambda pid, api_key=None: [])

    result = wf.push_prep_sections_to_agenda(
        agenda_id="agenda_123",
        sections=[
            {"label": "🐷🔍 Framing", "items": ["one-line framing"]},
            {"label": "📢 Evidence", "items": ["a", "b", "c"]},
        ],
        api_key="k",
    )

    assert set(result["created"]) == {"🐷🔍 Framing", "📢 Evidence"}
    assert result["skipped"] == []
    # 2 section parents + 1 framing item + 3 evidence items = 6 create calls
    assert len(fake.calls) == 6
    # Every call must use position="bottom"
    for c in fake.calls:
        assert c["position"] == "bottom"


def test_push_prep_sections_skips_labels_already_present(monkeypatch):
    """If a section label already exists under Agenda, skip it entirely
    — idempotency rule. Re-running the push should not duplicate."""
    fake = _FakeAPI()
    existing = [
        {"id": "ex_framing", "name": "🐷🔍 Framing", "parent_id": "agenda_123"},
        {"id": "ex_evidence", "name": "📢 Evidence", "parent_id": "agenda_123"},
    ]
    monkeypatch.setattr(wf, "create_node", fake.create_node)
    monkeypatch.setattr(
        wf, "get_children",
        lambda pid, api_key=None: existing if pid == "agenda_123" else [],
    )

    result = wf.push_prep_sections_to_agenda(
        agenda_id="agenda_123",
        sections=[
            {"label": "🐷🔍 Framing", "items": ["fresh framing"]},
            {"label": "📢 Evidence", "items": ["a"]},
            {"label": "🎤 Pitch", "items": ["a new pitch"]},
        ],
        api_key="k",
    )

    assert result["created"] == ["🎤 Pitch"]
    assert set(result["skipped"]) == {"🐷🔍 Framing", "📢 Evidence"}
    # Only 2 calls: Pitch section + its 1 item.
    assert len(fake.calls) == 2


def test_push_prep_sections_skips_empty_item_lists(monkeypatch):
    """Sections with no items are skipped entirely (not emitted as
    empty parents)."""
    fake = _FakeAPI()
    monkeypatch.setattr(wf, "create_node", fake.create_node)
    monkeypatch.setattr(wf, "get_children", lambda pid, api_key=None: [])

    result = wf.push_prep_sections_to_agenda(
        agenda_id="agenda_123",
        sections=[
            {"label": "🎤 Pitch", "items": ["a pitch"]},
            {"label": "🚩 Red flags", "items": []},  # empty — skip
        ],
        api_key="k",
    )
    assert result["created"] == ["🎤 Pitch"]
    assert result["skipped"] == []  # empty != skipped; just not created
    assert len(fake.calls) == 2  # one for Pitch section, one for item


# ---------------------------------------------------------------------------
# derive_meeting_title — title/hashtags derivation from a prep dict.
#
# Problem: recruiter invites from big-company in-house ATS systems
# (Adobe, Amazon, Google) carry no attendees on the invite — the
# interviewer is only named in the description body. Fall back to
# parsing the description + organizer-domain to get a useful title
# instead of the previous 'Unknown #JobSearch'.
# ---------------------------------------------------------------------------


def _prep(**overrides):
    """Minimal prep-cache shape for derivation tests. Mirrors what
    meeting-prep.py writes: attendees list, context.description,
    top-level organizer/location, self_context.target_company."""
    base = {
        "attendees": [],
        "context": {"description": ""},
        "organizer": None,
        "location": None,
        "self_context": {"target_company": None},
    }
    base.update(overrides)
    return base


def test_derive_title_prefers_named_attendee():
    prep = _prep(attendees=[{"name": "Michelle Leist", "email": ""}])
    title, tags = wf.derive_meeting_title(prep)
    assert title == "Michelle Leist"
    assert "JobSearch" in tags


def test_derive_title_from_description_interviewer():
    """Adobe-style invite: no attendees, interviewer in description."""
    prep = _prep(
        context={"description": (
            "Your meeting time has been confirmed.\n"
            "Adobe Leadership Chat (30 minutes)\n"
            "Interviewer(s): Alyssa Bonefas\n"
        )},
        organizer="schedule@interview.adobe.com",
    )
    title, tags = wf.derive_meeting_title(prep)
    assert title == "Alyssa Bonefas"
    assert "JobSearch" in tags
    assert "Adobe" in tags


def test_derive_title_from_description_with_pattern():
    """Alternative phrasing: 'With: <name>'."""
    prep = _prep(
        context={"description": "Phone screen\nWith: Jane Cooper\n"},
        organizer="scheduler@recruiting.amazon.com",
    )
    title, tags = wf.derive_meeting_title(prep)
    assert title == "Jane Cooper"
    assert "Amazon" in tags


def test_derive_title_from_description_host_pattern():
    prep = _prep(
        context={"description": "Intro call\nHost: Priya Sharma\n"},
    )
    title, _ = wf.derive_meeting_title(prep)
    assert title == "Priya Sharma"


def test_derive_company_strips_ats_subdomain():
    """'schedule@interview.adobe.com' → company 'Adobe', not
    'Interview' (the ATS prefix) and not 'interview.adobe' (raw)."""
    prep = _prep(organizer="schedule@interview.adobe.com")
    _, tags = wf.derive_meeting_title(prep)
    assert "Adobe" in tags


def test_derive_company_from_plain_ats_domain():
    """'hire.withgoogle.com' has no ATS prefix but IS a RECRUITER_DOMAINS
    entry — skip the company tag (Google is the host here, but the operator
    might be interviewing anywhere; don't guess)."""
    prep = _prep(organizer="no-reply@hire.withgoogle.com")
    _, tags = wf.derive_meeting_title(prep)
    # Don't misattribute "withgoogle" as the company.
    assert "Withgoogle" not in tags
    assert "Hire" not in tags


def test_derive_company_target_wins_over_organizer():
    """When the operator has a curated target_company, trust that over
    the organizer-domain inference (operator > heuristic)."""
    prep = _prep(
        organizer="schedule@interview.adobe.com",
        self_context={"target_company": {"company": "Adobe Inc."}},
    )
    _, tags = wf.derive_meeting_title(prep)
    assert "AdobeInc" in tags  # existing CamelCase convention
    assert "Adobe" not in tags  # don't duplicate


def test_derive_title_from_description_signoff():
    """Cold-recruiter LinkedIn pattern (2026-04-22 Coinbase/Abby):
    no attendees, no 'Interviewer:' header, but the description ends
    with a sign-off + name. Extract the sign-off name as the title
    rather than falling back to 'Unknown'."""
    prep = _prep(
        context={"description": (
            "Hi the operator,\n\nFollowing up on our LinkedIn conversation! "
            "Looking forward to connecting tomorrow 4/23 at 12:30pm. "
            "I've sent a calendar invite and a Google Meet link.\n\n"
            "Best,\n\nAbby"
        )},
        organizer="sam.smith@example.com",
    )
    title, _ = wf.derive_meeting_title(prep)
    assert title == "Abby"


def test_derive_title_signoff_handles_full_name():
    """Sign-off with first + last — preserve both."""
    prep = _prep(
        context={"description": (
            "Hi the operator, reaching out about a Senior DS role.\n\n"
            "Thanks,\nMegan Whitley"
        )},
    )
    title, _ = wf.derive_meeting_title(prep)
    assert title == "Megan Whitley"


def test_derive_title_signoff_ignores_greeting_name():
    """Sign-off extraction must not grab 'the operator' from 'Hi the operator,' —
    the greeting names the recipient, not the sender."""
    prep = _prep(
        context={"description": (
            "Hi the operator,\n\nPlease confirm.\n\nBest,\nSarah"
        )},
    )
    title, _ = wf.derive_meeting_title(prep)
    assert title == "Sarah"


def test_derive_company_from_event_title_coinbase_pattern():
    """'Interview with Coinbase' — the company is in the event
    summary, not the organizer domain or target list. Use the word
    after 'with' as a company hashtag when no ATS / target signal
    is present."""
    prep = _prep(
        title="Interview with Coinbase",
        context={"description": "Best,\nAbby"},
        organizer="sam.smith@example.com",
    )
    _, tags = wf.derive_meeting_title(prep)
    assert "Coinbase" in tags


def test_derive_company_from_event_title_slash_pattern():
    """'Coinbase / the operator' — company on the left of a slash."""
    prep = _prep(
        title="Coinbase / the operator",
        organizer="sam.smith@example.com",
    )
    _, tags = wf.derive_meeting_title(prep)
    assert "Coinbase" in tags


def test_derive_company_from_event_title_skips_generic_words():
    """Don't let 'Interview with the operator' promote 'the operator' to a hashtag,
    and don't let 'Meeting with team' promote 'team'. Filter common
    noise words."""
    prep = _prep(
        title="Meeting with team",
        organizer="sam.smith@example.com",
    )
    _, tags = wf.derive_meeting_title(prep)
    assert "Team" not in tags


def test_derive_title_unknown_when_no_signal():
    """No attendees, no description patterns, no organizer → fall
    back to the existing 'Unknown' behavior."""
    prep = _prep(
        context={"description": "Random event text with no hints."},
    )
    title, tags = wf.derive_meeting_title(prep)
    assert title == "Unknown"
    assert tags == ["JobSearch"]


def test_derive_title_ignores_empty_attendee_name():
    """Attendee with empty name string shouldn't short-circuit to
    empty title — keep falling back."""
    prep = _prep(
        attendees=[{"name": "", "email": "x@y.com"}],
        context={"description": "Interviewer(s): Sam Rivera"},
    )
    title, _ = wf.derive_meeting_title(prep)
    assert title == "Sam Rivera"


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
        root_id="notes_root",
        tz="America/Los_Angeles",
    )
    # First create should be the meeting node itself under notes_apr22.
    meeting_creates = [
        c for c in fake.calls if c["parent_id"] == "notes_apr22"
    ]
    assert meeting_creates
    assert "Michelle Leist" in meeting_creates[0]["name"]
    assert "#JobSearch" in meeting_creates[0]["name"]
    assert "#Reddit" in meeting_creates[0]["name"]
