"""Tests for search_status_lib — pure helpers for the active-search
pipeline miner. Runner lives in search-status-build.py; this tests
the evidence extraction + prompt building + response parsing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from search_status_lib import (  # type: ignore
    STAGES,
    build_status_prompt,
    extract_gmail_evidence,
    extract_workflowy_evidence,
    format_status_md,
    is_interview_workflowy_record,
    parse_status_response,
)


# --- STAGES sanity ---

def test_stages_covers_funnel_from_initial_to_decision():
    for s in ["initial-outreach", "recruiter-screen", "hiring-manager",
              "technical-interview", "onsite-panel", "final-round",
              "offer", "accepted", "declined", "passed", "ghosted"]:
        assert s in STAGES


# --- extract_gmail_evidence ---

def test_extract_gmail_evidence_from_thread_list():
    """Input: list of thread dicts with messages; Output: list of
    per-thread evidence with {thread_id, subject, from, to, date,
    message_count, snippet}."""
    threads = [
        {
            "id": "t1",
            "messages": [
                {"date": "2026-04-15T10:00:00Z",
                 "sender": "jane@greenhouse-mail.io",
                 "subject": "Senior DS Director role",
                 "snippet": "Reaching out about a role at Stripe..."},
                {"date": "2026-04-16T14:00:00Z",
                 "sender": "sam.smith+backup@example.com",
                 "subject": "Re: Senior DS Director role",
                 "snippet": "Happy to chat — proposing Tuesday..."},
            ],
        },
    ]
    evidence = extract_gmail_evidence(threads)
    assert len(evidence) == 1
    e = evidence[0]
    assert e["thread_id"] == "t1"
    assert "Stripe" in e["snippet"] or "Senior DS Director" in e["subject"]
    assert e["message_count"] == 2
    assert e["last_date"] == "2026-04-16T14:00:00Z"


def test_extract_gmail_evidence_captures_first_and_last_sender():
    """Knowing who started + who last sent tells us stage (the operator replied
    last → engaged; recruiter replied last → ball in the operator's court)."""
    threads = [{
        "id": "t1",
        "messages": [
            {"date": "2026-04-10T10:00:00Z", "sender": "rec@acme.com",
             "subject": "s", "snippet": "..."},
            {"date": "2026-04-11T10:00:00Z", "sender": "operator@gmail.com",
             "subject": "Re: s", "snippet": "..."},
            {"date": "2026-04-12T10:00:00Z", "sender": "rec@acme.com",
             "subject": "Re: s", "snippet": "..."},
        ],
    }]
    evidence = extract_gmail_evidence(threads)[0]
    assert evidence["first_sender"] == "rec@acme.com"
    assert evidence["last_sender"] == "rec@acme.com"
    assert evidence["message_count"] == 3


def test_extract_gmail_evidence_handles_empty_threads():
    assert extract_gmail_evidence([]) == []
    assert extract_gmail_evidence([{"id": "t1", "messages": []}]) == []


# --- is_interview_workflowy_record ---

@pytest.mark.parametrize("title,expected", [
    ("Stripe phone screen prep", True),
    ("Uber onsite prep", True),
    ("Anthropic loop debrief", True),
    ("1:1 with Dan Schmierer", False),
    ("Weekly standup", False),
    ("Pricing strategy review", False),
    ("Interview loop - Wayfair Director", True),
    ("Recruiter screen - Doordash", True),
    ("Hiring manager intro - Riot", True),
    ("Final round prep", True),
])
def test_is_interview_workflowy_record(title, expected):
    rec = {"text_excerpt": title + "\nsome notes"}
    assert is_interview_workflowy_record(rec) is expected


# --- extract_workflowy_evidence ---

def test_extract_workflowy_evidence_filters_to_interview_records():
    wf_records = [
        {"path": "wf://1", "chronological_date": "2026-04-15",
         "summary": "Stripe phone screen prep — rehearsed decision frameworks",
         "text_excerpt": "Stripe phone screen prep\nDecision frameworks rehearsed"},
        {"path": "wf://2", "chronological_date": "2026-04-10",
         "summary": "Weekly airbnb standup",
         "text_excerpt": "Weekly standup\nAgenda items..."},
        {"path": "wf://3", "chronological_date": "2026-04-18",
         "summary": "Uber onsite prep — heterogeneous supply framework",
         "text_excerpt": "Uber onsite prep\nHeterogeneous supply framework..."},
    ]
    evidence = extract_workflowy_evidence(wf_records)
    assert len(evidence) == 2
    paths = [e["path"] for e in evidence]
    assert "wf://1" in paths
    assert "wf://3" in paths
    assert "wf://2" not in paths


# --- build_status_prompt ---

def test_build_status_prompt_mentions_stages():
    prompt = build_status_prompt(gmail_evidence=[], workflowy_evidence=[])
    for s in ["initial-outreach", "onsite-panel", "offer", "declined"]:
        assert s in prompt


def test_build_status_prompt_includes_evidence_when_provided():
    gmail_ev = [{
        "thread_id": "t1",
        "subject": "Senior DS at Wayfair",
        "first_sender": "recruiter@wayfair.com",
        "last_sender": "operator@gmail.com",
        "last_date": "2026-04-18T10:00:00Z",
        "message_count": 4,
        "snippet": "Looking forward to meeting Fiona next week.",
    }]
    wf_ev = [{
        "path": "wf://1",
        "chronological_date": "2026-04-18",
        "summary": "Wayfair onsite debrief notes",
    }]
    prompt = build_status_prompt(gmail_ev, wf_ev)
    assert "Wayfair" in prompt
    assert "Fiona" in prompt
    assert "onsite debrief" in prompt


def test_build_status_prompt_requests_structured_json():
    prompt = build_status_prompt(gmail_evidence=[], workflowy_evidence=[])
    assert "company" in prompt
    assert "stage" in prompt
    assert "last_signal_date" in prompt


# --- parse_status_response ---

def test_parse_status_response_valid_json():
    raw = json.dumps({
        "active_searches": [
            {"company": "Wayfair", "stage": "onsite-panel",
             "last_signal_date": "2026-04-18", "notes": "CTO conversation"},
            {"company": "Uber", "stage": "final-round",
             "last_signal_date": "2026-04-15", "notes": "Heterogeneous supply framework"},
        ],
        "recently_concluded": [
            {"company": "Stripe", "outcome": "passed", "date": "2026-03-10",
             "notes": "Level mismatch"},
        ],
    })
    result = parse_status_response(raw)
    assert len(result["active_searches"]) == 2
    assert result["active_searches"][0]["company"] == "Wayfair"
    assert len(result["recently_concluded"]) == 1


def test_parse_status_response_strips_code_fences():
    raw = '```json\n{"active_searches": [], "recently_concluded": []}\n```'
    result = parse_status_response(raw)
    assert result["active_searches"] == []


def test_parse_status_response_coerces_unknown_stage_to_unknown():
    raw = json.dumps({
        "active_searches": [{"company": "X", "stage": "made-up-stage",
                             "last_signal_date": "2026-04-01"}],
        "recently_concluded": [],
    })
    result = parse_status_response(raw)
    assert result["active_searches"][0]["stage"] == "unknown"


def test_parse_status_response_invalid_json_returns_empty():
    """Graceful: LLM failure shouldn't crash the synthesis layer."""
    result = parse_status_response("not json")
    assert result == {"active_searches": [], "recently_concluded": []}


# --- format_status_md ---

def test_format_status_md_renders_active_table():
    data = {
        "active_searches": [
            {"company": "Wayfair", "stage": "onsite-panel",
             "last_signal_date": "2026-04-18", "notes": "CTO call scheduled"},
        ],
        "recently_concluded": [],
    }
    md = format_status_md(data, generated_at="2026-04-21T18:00:00+00:00")
    assert "Wayfair" in md
    assert "onsite-panel" in md
    assert "2026-04-18" in md
    assert "CTO call scheduled" in md
    assert "Active pipeline" in md or "Active searches" in md


def test_format_status_md_handles_empty_pipeline():
    data = {"active_searches": [], "recently_concluded": []}
    md = format_status_md(data, generated_at="2026-04-21T18:00:00+00:00")
    assert isinstance(md, str)
    # Should still render a header + a note about empty state
    assert len(md) > 0


def test_format_status_md_includes_concluded_section():
    data = {
        "active_searches": [],
        "recently_concluded": [
            {"company": "Stripe", "outcome": "passed", "date": "2026-03-10",
             "notes": "Level mismatch"},
        ],
    }
    md = format_status_md(data, generated_at="2026-04-21T18:00:00+00:00")
    assert "Stripe" in md
    assert "passed" in md.lower() or "Passed" in md
