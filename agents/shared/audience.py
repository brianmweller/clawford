"""Audience scope utilities for knowledge-fact visibility.

Ported verbatim from Flux/flux/memory/audience.py. Huckle Cat uses this to
gate which brain facts may reach a given recipient before a draft is composed.

Behavior contract (must match Flux exactly so imported facts work unchanged):
  - A fact with no audience_scope (None or []) is visible to ALL audiences.
  - A fact with a scope is visible iff its scope intersects the target audiences.

Call sites:
  - agents/connector/scripts/draft-compose.py filters brain.get_facts(subject=X)
    through fact_visible_to_audience() before including any fact in the LLM
    prompt for an email reply.
"""
from __future__ import annotations


RELATIONSHIP_TO_AUDIENCE: dict[str, list[str]] = {
    "colleague":     ["professional"],
    "manager":       ["professional"],
    "direct_report": ["professional"],
    "client":        ["professional"],
    "vendor":        ["professional"],
    "partner":       ["professional"],
    "friend":        ["personal", "friends"],
    "family":        ["personal", "family"],
    "acquaintance":  ["personal"],
    "mentor":        ["professional", "personal"],
    "mentee":        ["professional", "personal"],
}


DIRECTION_DEFAULTS: dict[str, dict] = {
    "manager":       {"direction": "upward",   "power_diff": -0.5, "social_dist": 0.3},
    "direct_report": {"direction": "downward", "power_diff":  0.5, "social_dist": 0.3},
    "colleague":     {"direction": "lateral",  "power_diff":  0.0, "social_dist": 0.4},
    "client":        {"direction": "external", "power_diff": -0.3, "social_dist": 0.5},
    "vendor":        {"direction": "external", "power_diff":  0.3, "social_dist": 0.6},
    "partner":       {"direction": "lateral",  "power_diff":  0.0, "social_dist": 0.4},
    "friend":        {"direction": "personal", "power_diff":  0.0, "social_dist": 0.1},
    "family":        {"direction": "personal", "power_diff":  0.0, "social_dist": 0.0},
    "acquaintance":  {"direction": "external", "power_diff":  0.0, "social_dist": 0.7},
    "mentor":        {"direction": "upward",   "power_diff": -0.3, "social_dist": 0.2},
    "mentee":        {"direction": "downward", "power_diff":  0.3, "social_dist": 0.2},
}


def audiences_for_recipient(relationship_type: str | None) -> list[str]:
    if not relationship_type:
        return ["professional"]
    return RELATIONSHIP_TO_AUDIENCE.get(relationship_type, ["professional"])


def defaults_for_relationship(relationship_type: str | None) -> dict:
    if not relationship_type:
        return {"direction": "lateral", "power_diff": 0.0, "social_dist": 0.5}
    return DIRECTION_DEFAULTS.get(
        relationship_type,
        {"direction": "lateral", "power_diff": 0.0, "social_dist": 0.5},
    )


def fact_visible_to_audience(
    fact_scope: list[str] | None,
    target_audiences: list[str],
) -> bool:
    if not fact_scope:
        return True
    return bool(set(fact_scope) & set(target_audiences))
