"""Audience-scope visibility and relationship-defaults for fact retrieval.

This module is the confidentiality gate used when Huckle Cat (connector)
composes an email reply: facts whose audience_scope does not overlap with
the recipient's audience tags must not reach the draft composer.

Ported from Flux/flux/memory/audience.py. Behavior is intentionally identical
so that facts imported from Flux (already tagged) work unchanged.
"""
from __future__ import annotations

from agents.shared.audience import (
    audiences_for_recipient,
    defaults_for_relationship,
    fact_visible_to_audience,
)


# --- fact_visible_to_audience ---

def test_unscoped_fact_visible_to_any_audience():
    assert fact_visible_to_audience(None, ["professional"])
    assert fact_visible_to_audience([], ["family"])


def test_scoped_fact_visible_when_scope_overlaps():
    assert fact_visible_to_audience(["personal", "family"], ["family"])
    assert fact_visible_to_audience(["professional"], ["professional", "internal"])


def test_scoped_fact_invisible_when_no_overlap():
    assert not fact_visible_to_audience(["personal"], ["professional"])
    assert not fact_visible_to_audience(["family"], ["professional"])


def test_scoped_fact_invisible_when_target_empty():
    assert not fact_visible_to_audience(["personal"], [])


# --- audiences_for_recipient ---

def test_known_relationship_returns_mapped_audiences():
    assert audiences_for_recipient("family") == ["personal", "family"]
    assert audiences_for_recipient("colleague") == ["professional"]
    assert audiences_for_recipient("friend") == ["personal", "friends"]


def test_unknown_relationship_defaults_to_professional():
    assert audiences_for_recipient("some-unmapped-type") == ["professional"]


def test_none_relationship_defaults_to_professional():
    assert audiences_for_recipient(None) == ["professional"]


def test_mentor_spans_professional_and_personal():
    # Mentors see both work and life-adjacent context (per Flux).
    assert audiences_for_recipient("mentor") == ["professional", "personal"]


# --- defaults_for_relationship ---

def test_manager_defaults_to_upward_negative_power():
    d = defaults_for_relationship("manager")
    assert d["direction"] == "upward"
    assert d["power_diff"] < 0      # manager has more power → speaker is below
    assert 0 < d["social_dist"] < 0.5


def test_family_is_zero_distance_personal():
    d = defaults_for_relationship("family")
    assert d["direction"] == "personal"
    assert d["social_dist"] == 0.0


def test_unknown_relationship_defaults_to_lateral_center():
    d = defaults_for_relationship("xyz-unknown")
    assert d == {"direction": "lateral", "power_diff": 0.0, "social_dist": 0.5}


def test_none_relationship_defaults_to_lateral_center():
    d = defaults_for_relationship(None)
    assert d["direction"] == "lateral"
    assert d["power_diff"] == 0.0


# --- integration with Huckle people-file circles ---

def test_huckle_people_circles_resolve_to_audiences():
    """Huckle's people files tag circles (family-inner, professional-outer...).
    These should route cleanly through relationship_type mapping when a person
    record supplies one.
    """
    # family-inner people typically carry relationship_type="family"
    assert "family" in audiences_for_recipient("family")
    # professional-outer people typically carry "colleague" / "vendor" / "client"
    assert "professional" in audiences_for_recipient("colleague")
    assert "professional" in audiences_for_recipient("vendor")
    assert "professional" in audiences_for_recipient("client")
