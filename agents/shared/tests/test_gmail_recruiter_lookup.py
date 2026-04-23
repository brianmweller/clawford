"""Tests for agents/shared/gmail_recruiter_lookup.py.

Covers the Coinbase/Abby regression (2026-04-22): calendar invite
description signs off 'Best, Abby' with no last name anywhere in the
calendar data. The separate Gmail thread from the recruiter carries
'From: Abby Mintert <abby@coinbase.com>' which is the canonical source
of truth for her full name + email.
"""
from __future__ import annotations

import pytest

from agents.shared.gmail_recruiter_lookup import (
    _extract_company_token,
    _extract_meet_url,
    _name_matches_first,
    _parse_from_header,
    resolve_recruiter_from_gmail,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_extract_meet_url_from_description():
    event = {
        "description": (
            "Hi the operator, Following up on our LinkedIn conversation!\n\n"
            "Google Meet: https://meet.google.com/cww-njwm-ksx\n\n"
            "Best,\nAbby"
        ),
    }
    assert _extract_meet_url(event) == "https://meet.google.com/cww-njwm-ksx"


def test_extract_meet_url_prefers_conference_link_field():
    """gcal-fetch populates conference_link directly — trust it over
    description scraping."""
    event = {
        "conference_link": "https://meet.google.com/abc-defg-hij",
        "description": "fallback https://meet.google.com/other-xxx-yyy",
    }
    assert _extract_meet_url(event) == "https://meet.google.com/abc-defg-hij"


def test_extract_meet_url_empty_when_missing():
    assert _extract_meet_url({"description": "no video link here"}) == ""
    assert _extract_meet_url({}) == ""


def test_extract_company_token_with_pattern():
    assert _extract_company_token({"summary": "Interview with Coinbase"}) == "Coinbase"


def test_extract_company_token_slash_pattern():
    assert _extract_company_token({"summary": "Coinbase / the operator"}) == "Coinbase"


def test_extract_company_token_prefers_summary_over_title():
    # gcal-fetch uses 'summary'; tests/internal dicts use 'title'. Both work.
    assert _extract_company_token({"title": "Interview with Anthropic"}) == "Anthropic"


def test_parse_from_header_display_name():
    name, email = _parse_from_header('"Abby Mintert" <abby@coinbase.com>')
    assert name == "Abby Mintert"
    assert email == "abby@coinbase.com"


def test_parse_from_header_bare_address():
    name, email = _parse_from_header("abby@coinbase.com")
    assert name == ""
    assert email == "abby@coinbase.com"


def test_name_matches_first_exact():
    assert _name_matches_first("Abby Mintert", "Abby") is True


def test_name_matches_first_case_insensitive():
    assert _name_matches_first("abby mintert", "ABBY") is True


def test_name_matches_first_rejects_prefix_only():
    """'Abigail Mintert' must NOT match hint 'Abby' — first TOKEN
    equality, not startswith. Otherwise 'the operator' would match 'Bri'."""
    assert _name_matches_first("Abigail Mintert", "Abby") is False


def test_name_matches_first_empty_hint():
    assert _name_matches_first("Any Name", "") is False


# ---------------------------------------------------------------------------
# resolve_recruiter_from_gmail — with a stub service
# ---------------------------------------------------------------------------


class _StubGmail:
    """Minimal Gmail service-like object. Records every .list query and
    returns caller-provided message ids / From headers."""

    def __init__(self, *, list_responses: dict[str, list[str]],
                 headers: dict[str, str]):
        self.list_responses = list_responses
        self.headers = headers
        self.queries: list[str] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, *, userId, q, maxResults):
        self.queries.append(q)
        ids = self.list_responses.get(q, [])
        return _Exec({"messages": [{"id": i} for i in ids]})

    def get(self, *, userId, id, format, metadataHeaders):
        header = self.headers.get(id, "")
        return _Exec({
            "payload": {"headers": [{"name": "From", "value": header}]}
        })


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def test_resolve_uses_meet_url_first_and_finds_abby():
    """Google Meet URL is the unique signal — Gmail returns one thread
    with 'Abby Mintert <abby@coinbase.com>' in the From: header."""
    meet = "https://meet.google.com/cww-njwm-ksx"
    event = {
        "summary": "Interview with Coinbase",
        "description": f"Google Meet: {meet}\n\nBest,\nAbby",
    }
    service = _StubGmail(
        list_responses={f'"{meet}"': ["msg-1"]},
        headers={"msg-1": '"Abby Mintert" <abby@coinbase.com>'},
    )

    result = resolve_recruiter_from_gmail(
        service=service,
        event=event,
        first_name_hint="Abby",
        operator_emails={"sam.smith@example.com"},
    )

    assert result == {"name": "Abby Mintert", "email": "abby@coinbase.com"}
    # Meet URL was tried first — no need to fall through to company search.
    assert service.queries[0] == f'"{meet}"'


def test_resolve_falls_through_to_company_then_firstname():
    """Meet URL → no hits; first-name + company → hit. Verify all three
    query tiers run in order until one resolves."""
    event = {
        "summary": "Interview with Coinbase",
        "description": "Best,\nAbby",  # no Meet URL in this event
    }
    service = _StubGmail(
        list_responses={
            '"Abby" "Coinbase" newer_than:30d': ["msg-7"],
        },
        headers={"msg-7": "Abby Mintert <abby@coinbase.com>"},
    )

    result = resolve_recruiter_from_gmail(
        service=service, event=event, first_name_hint="Abby",
        operator_emails={"sam.smith@example.com"},
    )
    assert result == {"name": "Abby Mintert", "email": "abby@coinbase.com"}
    # No Meet URL → no Meet-URL query. First query fired should be the
    # first-name + company tier.
    assert service.queries[0] == '"Abby" "Coinbase" newer_than:30d'


def test_resolve_skips_messages_from_brian():
    """When the most recent matching thread is the operator's own forwarded
    note, skip it and keep looking at older threads."""
    meet = "https://meet.google.com/cww-njwm-ksx"
    event = {"summary": "Interview with Coinbase",
             "description": f"Google Meet: {meet}\n\nBest,\nAbby"}
    service = _StubGmail(
        list_responses={f'"{meet}"': ["msg-operator-fwd", "msg-actual"]},
        headers={
            "msg-operator-fwd": 'Sam Smith <sam.smith@example.com>',
            "msg-actual": '"Abby Mintert" <abby@coinbase.com>',
        },
    )

    result = resolve_recruiter_from_gmail(
        service=service, event=event, first_name_hint="Abby",
        operator_emails={"sam.smith@example.com"},
    )
    assert result == {"name": "Abby Mintert", "email": "abby@coinbase.com"}


def test_resolve_returns_none_when_no_match():
    """Gmail returns threads but none have a sender whose first name
    matches the hint → no result (don't guess)."""
    event = {"summary": "Interview with Coinbase",
             "description": "Best,\nAbby"}
    service = _StubGmail(
        list_responses={
            '"Abby" "Coinbase" newer_than:30d': ["msg-a"],
            '"Abby" newer_than:30d': ["msg-a"],
        },
        headers={"msg-a": '"Random Person" <random@example.com>'},
    )
    result = resolve_recruiter_from_gmail(
        service=service, event=event, first_name_hint="Abby",
        operator_emails=set(),
    )
    assert result is None


def test_resolve_short_circuits_on_empty_hint():
    """No first-name extracted from the invite → don't make Gmail calls
    (a company-only query would match every inbound email)."""
    service = _StubGmail(list_responses={}, headers={})
    result = resolve_recruiter_from_gmail(
        service=service,
        event={"summary": "Interview with Coinbase", "description": "no signoff"},
        first_name_hint="",
    )
    assert result is None
    assert service.queries == []


def test_resolve_rejects_first_name_prefix_false_match():
    """'Abigail Lansing' must not satisfy hint 'Abby' even though
    it starts with 'Ab'. Same guarantee as _name_matches_first."""
    meet = "https://meet.google.com/cww-njwm-ksx"
    event = {"summary": "Interview with Coinbase",
             "description": f"Meet: {meet}\nBest,\nAbby"}
    service = _StubGmail(
        list_responses={
            f'"{meet}"': ["msg-abigail"],
            '"Abby" "Coinbase" newer_than:30d': [],
            '"Abby" newer_than:30d': [],
        },
        headers={"msg-abigail": '"Abigail Lansing" <abigail@other.com>'},
    )
    result = resolve_recruiter_from_gmail(
        service=service, event=event, first_name_hint="Abby",
        operator_emails=set(),
    )
    assert result is None
