"""Recipient-context assembler for Huckle Cat's draft-compose flow.

Pure function. Takes:
  - a recipient person record (from people/*.md)
  - the full fact list about that person (from facts/YYYY-MM.md)
  - optional pre-fetched context blobs (Gmail thread history, Workflowy
    mentions, Krisp meeting summaries)

Returns a RecipientContext bundling what the draft-compose LLM is allowed
to reference and (for the Telegram reasoning summary) which fact IDs were
blocked by the audience filter.

The external fetchers (Gmail thread read, Workflowy mention search, Krisp
transcript pull) are kept out of this module so it stays testable without
any network access. draft-compose.py calls those fetchers first and then
passes results into build_recipient_context().
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agents.shared.audience import audiences_for_recipient, fact_visible_to_audience


@dataclass
class RecipientContext:
    recipient_person: dict
    email_history: list[dict] = field(default_factory=list)
    workflowy_mentions: list[str] = field(default_factory=list)
    meeting_summaries: list[dict] = field(default_factory=list)
    facts_shareable: list[dict] = field(default_factory=list)
    facts_blocked: list[str] = field(default_factory=list)
    target_audiences: list[str] = field(default_factory=list)


def build_recipient_context(
    recipient_person: dict,
    all_facts: list[dict],
    email_history: list[dict] | None = None,
    workflowy_mentions: list[str] | None = None,
    meeting_summaries: list[dict] | None = None,
    override_audiences: list[str] | None = None,
) -> RecipientContext:
    target_audiences = override_audiences or audiences_for_recipient(
        recipient_person.get("relationship_type")
    )

    shareable: list[dict] = []
    blocked: list[str] = []
    for fact in all_facts:
        if fact_visible_to_audience(fact.get("audience_scope"), target_audiences):
            shareable.append(fact)
        else:
            blocked.append(fact["id"])

    return RecipientContext(
        recipient_person=recipient_person,
        email_history=email_history or [],
        workflowy_mentions=workflowy_mentions or [],
        meeting_summaries=meeting_summaries or [],
        facts_shareable=shareable,
        facts_blocked=blocked,
        target_audiences=target_audiences,
    )
