"""Pure helpers for draft-compose.py.

build_compose_prompt: assembles the LLM prompt from a RecipientContext,
voice-guidance bundle, the inbound email, and optional availability slots.

parse_compose_result: normalizes the JSON the LLM returns and strips any
cited_fact_ids that weren't in the shareable set (defense against LLM
hallucinating a fact reference that was blocked by the audience filter).
"""
from __future__ import annotations

import json
from datetime import datetime

from agents.shared.context_builder import RecipientContext


_OUTPUT_SCHEMA_HINT = """\
Respond with a JSON object with exactly these fields:
{
  "draft_text": "<the email reply body, no subject line, no signature block>",
  "reasoning_summary": "<one sentence for the operator's Telegram ping — why this draft, what was weighed>",
  "cited_fact_ids": ["<fact id>", ...]   // only IDs you actually referenced
}
"""


def build_compose_prompt(
    context: RecipientContext,
    voice: dict,
    inbound: dict,
    availability_slots: list[tuple[datetime, datetime]] | None = None,
) -> str:
    person = context.recipient_person
    name = person.get("full_name") or person.get("slug", "them")

    facts_lines = []
    for f in context.facts_shareable:
        facts_lines.append(f"  - [{f['id']}] {f['content']}")
    facts_block = "\n".join(facts_lines) if facts_lines else "  (none available)"

    history_lines = []
    for m in context.email_history[-20:]:
        who = m.get("from", "?")
        snippet = (m.get("body", "") or "")[:300].replace("\n", " ")
        history_lines.append(f"  - {who}: {snippet}")
    history_block = "\n".join(history_lines) if history_lines else "  (no prior email with this recipient)"

    workflowy_block = "\n".join(f"  - {w}" for w in context.workflowy_mentions) or "  (none)"
    meetings_block = "\n".join(
        f"  - {m.get('date','?')}: {m.get('title','')}" for m in context.meeting_summaries
    ) or "  (none)"

    slots_section = ""
    if availability_slots:
        slot_lines = []
        for s, e in availability_slots:
            slot_lines.append(
                f"  - {s.strftime('%a %b %d %H:%M')}–{e.strftime('%H:%M %Z')}"
            )
        slots_section = "\nOPEN SLOTS (propose these if the sender asked to schedule):\n" + "\n".join(slot_lines) + "\n"

    return f"""You are drafting an email reply on the operator's behalf. Do NOT send it — the operator will review.

RECIPIENT
  Name: {name}
  Relationship: {person.get("relationship_type", "unknown")}
  Slug: {person.get("slug", "")}

VOICE CALIBRATION
  Register: {voice.get("register")} — {voice.get("register_guidance")}
  Politeness: {voice.get("politeness_strategy")} — {voice.get("politeness_guidance")}
  Direction: {voice.get("direction")} — {voice.get("direction_description")}
  Power: {voice.get("power_description")}
  Distance: {voice.get("distance_description")}

COMMUNICATION CONTEXT
  {voice.get("thread_guidance")}
  {voice.get("response_guidance")}
  {voice.get("sensitivity_guidance")}
  {voice.get("valence_guidance")}
  {voice.get("time_pressure_guidance")}
  {voice.get("audience_guidance")}

WHAT YOU MAY REFERENCE (facts that passed the audience filter — the ONLY
facts about this person you know; do not invent or assume others)
{facts_block}

EMAIL HISTORY WITH THIS RECIPIENT (last 20)
{history_block}

WORKFLOWY MENTIONS
{workflowy_block}

MEETINGS YOU'VE BOTH ATTENDED
{meetings_block}
{slots_section}
INBOUND EMAIL (the one you're replying to)
  From: {inbound.get("from_name", "")} <{inbound.get("from_email", "")}>
  Subject: {inbound.get("subject", "")}
  Received: {inbound.get("received_at", "")}

  {inbound.get("body", "")}

TASK
  Draft a reply. Match the operator's voice for this register/relationship. Don't re-explain
  anything already covered in EMAIL HISTORY. Only reference facts from the
  "WHAT YOU MAY REFERENCE" section — do not invent or guess at other facts.
  If the sender asked to schedule and OPEN SLOTS are listed, propose them.
  Keep it natural — this is an email, not a form letter.

{_OUTPUT_SCHEMA_HINT}"""


def parse_compose_result(llm_text: str, shareable_ids: set[str]) -> dict:
    try:
        parsed = json.loads(llm_text)
    except (json.JSONDecodeError, TypeError):
        return {"error": "llm returned non-json", "draft_text": "", "raw": llm_text}

    if not isinstance(parsed, dict):
        return {"error": "llm returned non-object", "draft_text": "", "raw": llm_text}

    draft = parsed.get("draft_text")
    if not isinstance(draft, str) or not draft.strip():
        return {"error": "missing draft_text", "draft_text": "", "raw": llm_text}

    reasoning = parsed.get("reasoning_summary", "")
    if not isinstance(reasoning, str):
        reasoning = ""

    cited = parsed.get("cited_fact_ids", [])
    if not isinstance(cited, list):
        cited = []
    cited_clean = [c for c in cited if isinstance(c, str) and c in shareable_ids]

    return {
        "draft_text": draft,
        "reasoning_summary": reasoning,
        "cited_fact_ids": cited_clean,
    }
