"""Tests for the fleet-wide conversational carve-out added to every
entry in `AGENT_ROLE_SUMMARIES` in `agents/shared/reviewer.py`.

Motivation — 2026-04-20: Huckle Cat (connector) replied conversationally
to the operator's Telegram DM *"Did you draft any emails today?"*. The message
persisted to the JSONL but the outbound reviewer DENIED the send:
*"The agent's role is to send relationship nudges/notes triage, not to
conduct a direct conversation reply like this."* The narrow role
summaries never affirmatively licensed conversational Q&A with the operator,
so the LLM classifier defaulted to DENY. Every agent in the fleet has
the same shape of role summary, so the fix is fleet-wide.

These tests pin the contract: all 6 agents must (a) affirmatively
license conversational Q&A with the operator, (b) still forbid sending to
external recipients. Wording clarifications for connector (its
"never auto-replies" clause was ambiguous and also factually stale —
auto-compose drafts Gmail replies daily) and news-digest (scope its
auto-reply clause to LinkedIn) are pinned as well.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

from reviewer import (  # noqa: E402
    AGENT_ROLE_SUMMARIES,
    _build_prompt,
    review_action,
    role_summary_for,
)


EXPECTED_AGENTS = {
    "fix-it",
    "shopping",
    "news-digest",
    "family-calendar",
    "meetings-coach",
    "connector",
}


# ---------------------------------------------------------------------------
# Roster shape
# ---------------------------------------------------------------------------


def test_roster_covers_every_deployed_agent() -> None:
    assert set(AGENT_ROLE_SUMMARIES.keys()) == EXPECTED_AGENTS


# ---------------------------------------------------------------------------
# Fleet-wide carve-out — every agent's summary must affirmatively
# license conversational Q&A with the operator about its own state.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_id", sorted(EXPECTED_AGENTS))
def test_summary_licenses_meta_questions_from_brian(agent_id: str) -> None:
    summary = AGENT_ROLE_SUMMARIES[agent_id].lower()
    # Must mention answering the operator's DMs about the agent's own state.
    assert "answers operator" in summary or "replies to operator" in summary, (
        f"{agent_id} role summary is missing the conversational "
        f"carve-out — see reviewer.py AGENT_ROLE_SUMMARIES."
    )
    # Must mention that the carve-out is scoped to the agent's own
    # activity / state / runs, not arbitrary conversation.
    assert any(
        kw in summary for kw in ("activity", "state", "runs", "pipeline")
    ), f"{agent_id} carve-out must be scoped to the agent's own state"


@pytest.mark.parametrize("agent_id", sorted(EXPECTED_AGENTS))
def test_summary_still_forbids_sending_to_external_recipients(
    agent_id: str,
) -> None:
    """The carve-out must not dilute the 'never email / WhatsApp / DM
    outsiders' rail — this is the one absolute invariant."""
    summary = AGENT_ROLE_SUMMARIES[agent_id].lower()
    # Each agent must say "never" something about non-the operator recipients.
    assert "never" in summary, (
        f"{agent_id} summary must preserve a 'never …' rail about "
        f"non-the operator recipients."
    )


# ---------------------------------------------------------------------------
# Agent-specific wording fixes
# ---------------------------------------------------------------------------


def test_connector_no_longer_has_ambiguous_never_auto_replies() -> None:
    """The literal phrase 'never auto-replies' triggered the 2026-04-20
    false-positive DENY because the classifier read conversational
    Telegram replies as 'auto-replies'. Replace with explicit language."""
    summary = AGENT_ROLE_SUMMARIES["connector"]
    assert "never auto-replies" not in summary.lower()


def test_connector_mentions_gmail_draft_composition() -> None:
    """Huckle Cat drafts Gmail replies (auto-compose cron). The role
    summary must say so, or the reviewer will DENY legit messages
    that reference draft activity."""
    summary = AGENT_ROLE_SUMMARIES["connector"].lower()
    assert "gmail" in summary and ("draft" in summary or "drafts" in summary)


def test_news_digest_scopes_auto_reply_clause_to_linkedin() -> None:
    """Lowly Worm's 'does not auto-reply' was ambiguous across
    LinkedIn and Telegram. The clause must be scoped to LinkedIn
    specifically, not a global 'never reply' stance."""
    summary = AGENT_ROLE_SUMMARIES["news-digest"].lower()
    # Either no "auto-reply" word at all, or it's adjacent to a
    # LinkedIn qualifier.
    if "auto-reply" in summary:
        # Look for "linkedin" within ~60 chars of "auto-reply".
        idx = summary.find("auto-reply")
        window = summary[max(0, idx - 60):idx + 60]
        assert "linkedin" in window, (
            "news-digest 'auto-reply' clause must be scoped to "
            "LinkedIn — found the phrase without a nearby LinkedIn "
            "qualifier."
        )


# ---------------------------------------------------------------------------
# End-to-end through _build_prompt — the reviewer LLM actually sees
# the carve-out text when classifying a meta-reply.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_id", sorted(EXPECTED_AGENTS))
def test_build_prompt_surfaces_carveout_to_classifier(agent_id: str) -> None:
    """Verify the role summary (with the new carve-out clause) flows
    through into the prompt the classifier actually reads. If this
    assertion fails, the classifier has no evidence of the carve-out
    and will keep denying meta-replies."""
    p = _build_prompt(
        agent_id=agent_id,
        action_kind="telegram_send",
        payload={"chat_id": 1, "text": "Last run was 3:30 AM PT — all clear."},
        role_summary=role_summary_for(agent_id),
        context=None,
    )
    assert "answers operator" in p.lower() or "replies to operator" in p.lower()


# ---------------------------------------------------------------------------
# Regression: a blatant external-send payload must still classify DENY
# even with the widened role. Carve-out ≠ free pass.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.ok = True
        self.text = text
        self.trace_id = ""
        self.error = ""


def _keyword_classifier(prompt: str, **_kw) -> _FakeResult:
    """A deterministic stand-in for the real LLM. Returns DENY when
    the payload text looks like an external send; otherwise SAFE.
    Lets us exercise the full review_action pipeline in tests
    without hitting the real model."""
    low = prompt.lower()
    external_markers = (
        "emailed your brother",
        "sent a whatsapp",
        "messaged alice directly",
        "posted to linkedin",
        "replied to the recruiter",
    )
    if any(m in low for m in external_markers):
        return _FakeResult("DENY — out of scope for this agent.")
    return _FakeResult("SAFE — fits the agent's role.")


@pytest.mark.parametrize("agent_id", sorted(EXPECTED_AGENTS))
def test_meta_question_reply_classifies_safe(agent_id: str) -> None:
    """A realistic Telegram reply about the agent's own state must
    NOT be denied."""
    v = review_action(
        agent_id=agent_id,
        action_kind="telegram_send",
        payload={
            "chat_id": 1,
            "text": (
                "My last scheduled run was 3:30 AM PT and completed "
                "cleanly. Nothing has landed since."
            ),
        },
        role_summary=role_summary_for(agent_id),
        infer_fn=_keyword_classifier,
    )
    assert v.verdict == "safe", (
        f"{agent_id}: meta-question reply classified {v.verdict!r}: "
        f"{v.reason!r}"
    )


@pytest.mark.parametrize("agent_id", sorted(EXPECTED_AGENTS))
def test_external_send_still_classifies_deny(agent_id: str) -> None:
    """Carve-out must not open the door to actual external sends."""
    v = review_action(
        agent_id=agent_id,
        action_kind="telegram_send",
        payload={
            "chat_id": 1,
            "text": (
                "I just emailed your brother for you this morning to "
                "let him know about dinner."
            ),
        },
        role_summary=role_summary_for(agent_id),
        infer_fn=_keyword_classifier,
    )
    assert v.verdict == "deny", (
        f"{agent_id}: external-send reply classified {v.verdict!r} "
        f"(expected deny): {v.reason!r}"
    )
