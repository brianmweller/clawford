"""Tests for the pin-list short-circuit in contact-aggregator.auto_circle().

The holiday-card-pins.csv should make any future mining run classify
a listed contact as professional-outer on first ingest, regardless of
meeting counts or recency. This makes reclassification durable.

2026-04-22 (Option A remap): target circle is professional-outer (90d)
rather than professional-inner (7d) — pin intent is holiday-card
retention, not weekly nudging.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


AGGREGATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "mine" / "contact-aggregator.py"
)


@pytest.fixture
def agg():
    spec = importlib.util.spec_from_file_location("agg_pinned", AGGREGATOR_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DEFAULT_CFG = {
    "circle_rules": {
        "high_meeting_threshold": 5,
        "high_meeting_recency_days": 30,
        "high_email_sent_threshold": 10,
        "high_whatsapp_threshold": 5,
    },
    "personal_domains": ["gmail.com"],
}


def test_pinned_email_returns_professional_outer_regardless_of_meetings(agg):
    pinned = {"emails": {"drew.branden@example.com"}, "name_slugs": set()}
    contact = {
        "email": "drew.branden@example.com",
        "name": "Drew Branden",
        "meeting_count": 0,
        "last_interaction": "2024-06-01",
    }
    assert agg.auto_circle(contact, DEFAULT_CFG, pinned) == "professional-outer"


def test_pinned_name_slug_match_when_email_missing(agg):
    pinned = {"emails": set(), "name_slugs": {"dan-zylberglejd"}}
    contact = {
        "email": "danzylber@gmail.com",
        "name": "Dan Zylberglejd",
        "meeting_count": 0,
        "last_interaction": None,
    }
    assert agg.auto_circle(contact, DEFAULT_CFG, pinned) == "professional-outer"


def test_pinned_overrides_active_collaborator_promotion(agg):
    """Pin-list short-circuit must beat the 5+ recent meetings rule —
    even an actively collaborating pinned contact drops to 90d cadence
    because the pin list encodes 'holiday-card retention', not 'weekly
    check-in' (Option A, 2026-04-22)."""
    pinned = {"emails": {"active.pin@example.com"}, "name_slugs": set()}
    contact = {
        "email": "active.pin@example.com",
        "name": "Active Pin",
        "meeting_count": 10,
        "last_interaction": __import__("datetime").date.today().isoformat(),
    }
    assert agg.auto_circle(contact, DEFAULT_CFG, pinned) == "professional-outer"


def test_unpinned_contact_falls_through_to_existing_rules(agg):
    pinned = {"emails": set(), "name_slugs": set()}
    contact = {
        "email": "stranger@example.com",
        "name": "Stranger Person",
        "meeting_count": 1,
        "last_interaction": "2026-04-10",
    }
    assert agg.auto_circle(contact, DEFAULT_CFG, pinned) == "professional-outer"


def test_existing_meeting_rule_still_promotes_without_pin(agg):
    """Heavy-meeting recent contact still lands in professional-inner
    via the legacy rule when not on the pin list."""
    pinned = {"emails": set(), "name_slugs": set()}
    contact = {
        "email": "frequent@example.com",
        "name": "Frequent Collab",
        "meeting_count": 7,
        "last_interaction": __import__("datetime").date.today().isoformat(),
    }
    assert agg.auto_circle(contact, DEFAULT_CFG, pinned) == "professional-inner"


def test_load_pinned_contacts_parses_csv(agg, tmp_path):
    csv_path = tmp_path / "holiday-card-pins.csv"
    csv_path.write_text(
        "name,email\n"
        "Drew Branden,drew.branden@example.com\n"
        "Dan Zylberglejd,dan.zylberglejd@example.com\n",
        encoding="utf-8",
    )
    pinned = agg.load_pinned_contacts(csv_path)
    assert "drew.branden@example.com" in pinned["emails"]
    assert "dan-zylberglejd" in pinned["name_slugs"]


def test_load_pinned_contacts_missing_file_returns_empty(agg, tmp_path):
    """Production defensive behaviour — if the CSV gets deleted or the
    path is wrong, don't crash the aggregator."""
    pinned = agg.load_pinned_contacts(tmp_path / "nonexistent.csv")
    assert pinned == {"emails": set(), "name_slugs": set()}
