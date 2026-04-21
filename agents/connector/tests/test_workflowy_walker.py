"""Tests for the Workflowy walker — Stage 1's second source alongside
filesystem archives. The walker identifies meeting nodes (whose parent
is a Workflowy date-heading node) and emits records in the same shape
as iter_file_records, so the downstream classifier pipeline is unchanged.

Chronological tagging is load-bearing: every record carries a
parent_date derived from the date-heading ancestor, which the classifier
uses as a high-confidence date source. Node-level last_modified tells
us when the meeting notes were last edited, NOT when the meeting
happened — so mtime is secondary to parent_date.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from workflowy_walker import (  # type: ignore
    extract_date_from_heading,
    flatten_descendants,
    is_date_heading,
    iter_meeting_records,
    strip_html,
)


# --- is_date_heading / extract_date_from_heading ---

def test_is_date_heading_matches_standard_format():
    assert is_date_heading("Tue, Feb 11, 2026") is True
    assert is_date_heading("Thu, Jul 4, 2024") is True
    assert is_date_heading("Sun, Dec 31, 2019") is True


def test_is_date_heading_rejects_non_date():
    assert is_date_heading("Meeting with Jane") is False
    assert is_date_heading("February 2026") is False
    assert is_date_heading("2024") is False
    assert is_date_heading("") is False
    assert is_date_heading(None) is False


def test_is_date_heading_tolerates_html_wrapper():
    # Workflowy stores dates inside <time> tags
    heading = '<time startYear="2026" startMonth="2" startDay="11">Tue, Feb 11, 2026</time>'
    assert is_date_heading(heading) is True


def test_extract_date_from_heading_returns_iso():
    assert extract_date_from_heading("Tue, Feb 11, 2026") == "2026-02-11"
    assert extract_date_from_heading("Wed, Jan 1, 2020") == "2020-01-01"
    assert extract_date_from_heading("Sun, Dec 31, 2019") == "2019-12-31"


def test_extract_date_from_heading_handles_html():
    assert extract_date_from_heading('<time>Thu, Jul 4, 2024</time>') == "2024-07-04"


def test_extract_date_from_heading_returns_empty_for_non_date():
    assert extract_date_from_heading("Meeting with Jane") == ""
    assert extract_date_from_heading("") == ""


# --- strip_html ---

def test_strip_html_removes_tags():
    assert strip_html("<b>hello</b>") == "hello"
    assert strip_html("<time>Tue, Feb 11, 2026</time>") == "Tue, Feb 11, 2026"


def test_strip_html_handles_plain_text():
    assert strip_html("plain text") == "plain text"


# --- flatten_descendants ---

def test_flatten_descendants_joins_children_text():
    node = {"id": "n1", "name": "1:1 with Jane"}
    children_map = {
        "n1": [
            {"id": "n2", "name": "Discussed Q2 goals", "parent_id": "n1"},
            {"id": "n3", "name": "Follow up on comp review", "parent_id": "n1"},
        ],
    }
    text = flatten_descendants(node, children_map)
    assert "1:1 with Jane" in text
    assert "Q2 goals" in text
    assert "Follow up" in text


def test_flatten_descendants_recurses_grandchildren():
    node = {"id": "n1", "name": "Offsite"}
    children_map = {
        "n1": [{"id": "n2", "name": "Day 1", "parent_id": "n1"}],
        "n2": [{"id": "n3", "name": "Strategy discussion", "parent_id": "n2"}],
    }
    text = flatten_descendants(node, children_map)
    assert "Offsite" in text
    assert "Day 1" in text
    assert "Strategy discussion" in text


def test_flatten_descendants_strips_html():
    node = {"id": "n1", "name": "<b>1:1</b> with Jane"}
    text = flatten_descendants(node, {})
    assert "1:1 with Jane" in text
    assert "<b>" not in text


def test_flatten_descendants_leaf_node():
    node = {"id": "n1", "name": "Solo meeting"}
    text = flatten_descendants(node, {})
    assert text.strip() == "Solo meeting"


# --- iter_meeting_records ---

def test_iter_meeting_records_identifies_meeting_under_date_heading():
    export = [
        {"id": "yr", "name": "2024", "parent_id": "root"},
        {"id": "mo", "name": "July 2024", "parent_id": "yr"},
        {"id": "dt", "name": "Thu, Jul 4, 2024", "parent_id": "mo"},
        {"id": "mtg", "name": "1:1 with Jane", "parent_id": "dt",
         "last_modified_at": "2024-07-05T10:00:00Z"},
        {"id": "bullet1", "name": "Discussed comp review", "parent_id": "mtg"},
    ]
    records = list(iter_meeting_records(export))
    assert len(records) == 1
    r = records[0]
    assert r["path"] == "wf://mtg"
    assert r["role"] == "meetings"
    assert r["ext"] == ".wf"
    assert r["parent_date"] == "2024-07-04"
    assert "1:1 with Jane" in r["text_excerpt"]
    assert "comp review" in r["text_excerpt"]
    assert r["mtime"] == "2024-07-05T10:00:00Z"


def test_iter_meeting_records_emits_multiple_meetings_per_date():
    export = [
        {"id": "dt", "name": "Tue, Feb 11, 2026", "parent_id": "root"},
        {"id": "m1", "name": "Standup", "parent_id": "dt"},
        {"id": "m2", "name": "1:1 with Alice", "parent_id": "dt"},
        {"id": "m3", "name": "Strategy review", "parent_id": "dt"},
    ]
    records = list(iter_meeting_records(export))
    assert len(records) == 3
    names = {r["text_excerpt"].split("\n")[0] for r in records}
    assert "Standup" in names
    assert "1:1 with Alice" in names
    assert "Strategy review" in names


def test_iter_meeting_records_skips_non_meeting_nodes():
    """Project trees, journal entries, anything not directly under a date-heading."""
    export = [
        {"id": "proj", "name": "My Projects", "parent_id": "root"},
        {"id": "p1", "name": "Brainstorm ideas", "parent_id": "proj"},
        {"id": "p2", "name": "Shopping list", "parent_id": "proj"},
    ]
    records = list(iter_meeting_records(export))
    assert records == []


def test_iter_meeting_records_works_with_html_wrapped_date():
    export = [
        {"id": "dt", "name": '<time startYear="2026" startMonth="2" startDay="11">Tue, Feb 11, 2026</time>', "parent_id": None},
        {"id": "mtg", "name": "Sync", "parent_id": "dt",
         "last_modified_at": "2026-02-12T09:00:00Z"},
    ]
    records = list(iter_meeting_records(export))
    assert len(records) == 1
    assert records[0]["parent_date"] == "2026-02-11"


def test_iter_meeting_records_falls_back_when_no_last_modified():
    """If a node has no last_modified timestamp, use parent_date as mtime
    (better than empty, since downstream idempotency keys off mtime)."""
    export = [
        {"id": "dt", "name": "Tue, Feb 11, 2026", "parent_id": None},
        {"id": "mtg", "name": "Sync", "parent_id": "dt"},
    ]
    records = list(iter_meeting_records(export))
    assert len(records) == 1
    assert records[0]["mtime"].startswith("2026-02-11")


def test_iter_meeting_records_size_reflects_text_length():
    export = [
        {"id": "dt", "name": "Tue, Feb 11, 2026", "parent_id": None},
        {"id": "m1", "name": "Short", "parent_id": "dt"},
    ]
    r = list(iter_meeting_records(export))[0]
    assert r["size"] == len(r["text_excerpt"])


def test_iter_meeting_records_handles_alt_last_modified_field():
    """Workflowy API has used different field names across versions
    (lastModified, last_modified, last_modified_at). Walker accepts any."""
    export = [
        {"id": "dt", "name": "Tue, Feb 11, 2026", "parent_id": None},
        {"id": "m1", "name": "Sync", "parent_id": "dt", "lastModified": "2026-02-12T09:00:00Z"},
    ]
    r = list(iter_meeting_records(export))[0]
    assert "2026-02-12" in r["mtime"]


def test_iter_meeting_records_empty_export():
    assert list(iter_meeting_records([])) == []


def test_iter_meeting_records_includes_descendants_in_excerpt():
    """Full meeting context — agenda items, takeaways, nested notes — must
    reach the classifier. Otherwise the classifier only sees the meeting
    title and can't tell a 1:1 from an offsite from a calibration."""
    export = [
        {"id": "dt", "name": "Tue, Feb 11, 2026", "parent_id": None},
        {"id": "mtg", "name": "Quarterly planning", "parent_id": "dt"},
        {"id": "agenda", "name": "Agenda", "parent_id": "mtg"},
        {"id": "a1", "name": "Review Q1 OKRs", "parent_id": "agenda"},
        {"id": "a2", "name": "Align on Q2 bets", "parent_id": "agenda"},
        {"id": "take", "name": "Takeaways", "parent_id": "mtg"},
        {"id": "t1", "name": "Pricing guidance launches April", "parent_id": "take"},
    ]
    r = list(iter_meeting_records(export))[0]
    assert "Q1 OKRs" in r["text_excerpt"]
    assert "Q2 bets" in r["text_excerpt"]
    assert "Pricing guidance" in r["text_excerpt"]
