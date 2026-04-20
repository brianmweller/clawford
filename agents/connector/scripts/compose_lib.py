"""Pure helpers for draft-compose.py.

build_compose_prompt: assembles the LLM prompt from a RecipientContext,
voice-guidance bundle, the inbound email, and optional availability slots.
The prompt forces a structured four-step reasoning pass — objective,
state/gap, strategy, theory-of-mind — BEFORE any prose is written. A draft
without an explicit intent is a pleasantry, not a reply; the shape of the
output enforces that discipline.

parse_compose_result: normalizes the JSON the LLM returns, validates the
four reasoning fields are populated, and strips any cited_fact_ids that
weren't in the shareable set (defense against the LLM hallucinating
references to audience-filtered facts).
"""
from __future__ import annotations

import json
from datetime import datetime

from agents.shared.context_builder import RecipientContext


_OUTPUT_SCHEMA_HINT = """\
Respond with a JSON object with EXACTLY these fields, in this order:
{
  "reply_needed":           true | false,
  "objective":              "<one sentence: what is the operator trying to achieve? If reply_needed=false, the objective is what the operator gains by NOT replying (preserving the recipient's frame, respecting their closeout, etc.).>",
  "current_state_and_gap":  "<two–three sentences: given the context, where does the operator stand relative to the objective, and what is missing to close the gap?>",
  "leverage":               "<two–three sentences: what SPECIFIC assets does the operator have here — named people who can vouch, prior moves already made, concrete shared context, proof points? Enumerate at least one. If there is genuinely no leverage, say so plainly.>",
  "strategy":               "<two–three sentences: the concrete tactical move. If reply_needed=true, this is what the draft will DO (MUST deploy the leverage). If reply_needed=false, this is why silence is the right move and what it protects.>",
  "recipient_model":        "<two–three sentences. Answer BOTH: (1) what are they expecting task-wise? and (2) what EMOTIONAL OUTCOME do they want from the reply — to feel appreciated, useful, heard, forgiven, reassured, etc.? Gift-givers want the gift to feel loved, not tolerated. Advice-givers want acknowledgment the advice landed. Well-wishers want engagement with what they said. Closeout-senders want the thread to end gracefully. If the draft nails the task but misses the emotional transaction, the reply reads as cold.>",
  "draft_text":             "<when reply_needed=true: the email reply body, no subject line, no signature block. Match the voice anchors from history LITERALLY — sentence length, contractions, hedging, sign-off. When reply_needed=false: empty string.>",
  "no_reply_fyi":           "<when reply_needed=false: one short sentence the operator will read on Telegram — what arrived, why no reply is needed, any watch-for-later note. When reply_needed=true: empty string.>",
  "reasoning_summary":      "<one sentence anchored in objective + strategy — what the operator reads on Telegram alongside the draft (or alongside no_reply_fyi) to decide whether to ship/override.>",
  "cited_fact_ids":         ["<fact id>", ...]
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
        date = m.get("date", "")
        body = (m.get("body", "") or "").strip()
        header = f"  [{date}] {who}:" if date else f"  {who}:"
        history_lines.append(header)
        # Preserve paragraph structure — don't truncate; the whole point of
        # history is the voice anchor + any named leverage inside it.
        for line in body.splitlines():
            history_lines.append(f"      {line}")
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

    profile_section = ""
    if voice.get("profile_present"):
        patterns = "\n".join(f"    - {p}" for p in voice.get("profile_patterns", [])) or "    (none)"
        antis = "\n".join(f"    - {a}" for a in voice.get("profile_anti_patterns", [])) or "    (none)"
        openings = ", ".join(f'"{o}"' for o in voice.get("profile_opening_phrases", [])) or "(none)"
        profile_section = (
            "\nLEARNED VOICE PROFILE (distilled from the operator's prior sent mail to people\n"
            "in this circle — these patterns OVERRIDE abstract register guidance when\n"
            "they disagree)\n"
            f"  Typical greeting:   {voice.get('profile_greeting')}\n"
            f"  Typical signoff:    {voice.get('profile_signoff')}\n"
            f"  Common patterns:\n{patterns}\n"
            f"  Anti-patterns (avoid these):\n{antis}\n"
            f"  Opening phrases to match: {openings}\n"
            f"  Distinctive trait:  {voice.get('profile_distinctive_traits')}\n"
        )

    return f"""You are drafting an email reply on the operator's behalf. Do NOT send it — the operator will review.

Not every inbound deserves a reply, and a draft without an explicit
objective is a pleasantry, not a reply. A draft without identified
leverage defaults to generic warmth. BEFORE writing any prose, you MUST
think through:

  0. REPLY NEEDED? — decide this AFTER working through steps 1–5 below.

     STRONG default to silence when:
       - The inbound is a warm closeout with no question (a thread
         concluding gracefully — silence IS the acknowledgment).
       - A decision has already landed and the sender is wrapping up
         ("good luck", "stay in touch", "door is open"). Replies here
         re-open threads the sender just closed.
       - The inbound is a stall that's self-resolving ("I'll get back
         to you next week") with no leverage available now.
       - Any FYI with no ask.

     Diagnostics (any one of these triggers reply_needed=false):
       (a) If the draft you'd write would naturally end with "no need
           to reply," that's a TELL that the reply itself shouldn't
           exist. the operator writing "no need to reply" is him apologizing
           for an email he didn't need to send.
       (b) Put yourself in the recipient's chair, at their most
           critical. Ask: "Was this email necessary?" and "Did I get
           anything from this email?" If the honest answer to either
           is no, the email shouldn't exist. Generic warmth, re-stated
           acknowledgment, or "I just wanted to say thanks again" all
           fail this test.
       (c) If the draft's content is already redundantly conveyed by
           silence + context already on the thread, silence wins.

     Default to reply when:
       - There's an explicit question, request, or scheduling ask.
       - There's concrete leverage available NOW that would advance the
         objective (e.g., a recruiter stall is the moment to re-surface
         vouchers — even though there's no explicit question).
       - Silence would credibly read as blowing them off (a warm offer
         from a senior contact, a direct personal outreach from someone
         close).

     Set reply_needed accordingly. The five reasoning steps below run
     REGARDLESS of which branch you take — they're how you decide, not
     work you only do when replying.


  1. OBJECTIVE / INTENT — what is the operator trying to achieve with
     this communication? Be specific and outcome-oriented. Not "reply
     warmly"; rather "keep the candidacy pipeline alive for future roles
     at this company" or "decline without burning the bridge" or "lock
     in a meeting this week to unblock X."
  2. CURRENT STATE AND GAP — given the brain context, email history, and
     inbound message: where does the operator stand relative to the objective,
     and what's missing to close the gap?
  3. LEVERAGE / ASSETS — what SPECIFIC assets does the operator have here? Named
     people who can vouch. Prior moves already made in this thread. Proof
     points. Shared context that's load-bearing. Enumerate at least one
     concrete item. If there is genuinely no leverage, say so plainly —
     that usually means the right move is restraint, not more words.
  4. STRATEGY — the concrete moves the draft will make. MUST explicitly
     deploy the leverage. What to name specifically, what to offer, what
     kind of ask to surface (if any), what to leave unsaid. "Rooting from
     the outside" is not a strategy — it accomplishes nothing. Every
     sentence in the draft must serve a concrete move.
  5. RECIPIENT MODEL (theory of mind) — how will this person read the
     message?

     Task dimension: what decision / information / action are they
     expecting? What reads warm vs. pushy vs. transactional?

     Emotional dimension: what outcome do they want to FEEL after
     reading the reply? This is almost always implicit. Examples:
       - Gift-giver wants the gift to feel loved, not tolerated
         (warmth about the thing itself, not just a decision).
       - Advice-giver / tip-sharer wants to feel useful (acknowledgment
         the advice landed, not just "thanks").
       - Well-wisher wants to feel heard (engage with what they said).
       - Closeout-sender wants the thread to end gracefully (match
         their energy; don't re-open what they closed).
       - Apology-sender wants to feel forgiven (lightness, not
         interrogation).
       - Check-in sender wants to feel the operator is OK (brief substantive
         update beats a deflective "I'm fine").

     A draft that nails the task but misses the emotional transaction
     reads as technically correct but cold. BOTH dimensions need to be
     served.

Only after you've worked through all five should you decide reply_needed
and, if true, draft. If false, populate no_reply_fyi with a one-line
summary for the operator's Telegram ping instead.

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
{profile_section}

WHAT YOU MAY REFERENCE (facts that passed the audience filter — the ONLY
facts about this person you know; do not invent or assume others)
{facts_block}

EMAIL HISTORY — VOICE ANCHOR (these are the operator's actual prior messages to
this person; match them LITERALLY — sentence length, contractions,
hedging rate, sign-off form. If the abstract register calibration above
disagrees with how the operator actually writes here, HISTORY WINS.

Voice anchors are CONTEXT-SPECIFIC, not one-size-fits-all. A terse
"Thanks for letting me know!" reply to a transactional tip is NOT the
right anchor to match when the inbound is a gift offer — even from the
same sender. Pick the anchor whose emotional context matches the
current inbound, not just the most recent one.)
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
  Work through the five-step reasoning, then decide reply_needed.
  If reply_needed=true, draft. Match the VOICE ANCHOR from history
  literally — if the operator is terse and uses contractions there, the draft
  should too. Don't re-explain anything already covered in the thread.
  Only reference facts from "WHAT YOU MAY REFERENCE" — do not invent.
  Every sentence must serve a concrete move from the strategy.

  SCHEDULING RULE: if the sender asked about timing (explicitly or
  implicitly — "free?", "when works?", "catch up?", "meet?"), the
  draft MUST end with a CONCRETE time proposal. Specific day(s) +
  time range. Never end with a conditional that defers commitment
  ("happy to if you're around," "let me know what works"). Two
  sources of times, in order:
    (a) Prefer OPEN SLOTS if listed AND they match the sender's
        constraints (e.g., if the sender said "after 4pm" and the
        slots are morning, they DO NOT match).
    (b) If no OPEN SLOTS match, propose FREEHAND based on the operator's
        typical preferences and the sender's constraints. Never
        emit no proposal at all. "Tuesday or Thursday 5-7pm works —
        want to book one?" beats "happy to grab dinner if you're
        around" every time.

  DATE RE-ANCHORING: if the inbound is stale (sent days or weeks ago)
  and references relative dates like "next week," re-anchor those to
  TODAY. "Next week" means the week starting Monday from today's
  perspective, not from the inbound's perspective. Stale references
  are not a reason to drop scheduling — they're a reason to quietly
  translate and propose a fresh window.

  If reply_needed=false, populate no_reply_fyi with one short sentence
  summarizing what arrived and why no reply is needed; leave draft_text
  empty.

{_OUTPUT_SCHEMA_HINT}"""


_REQUIRED_REASONING_FIELDS = ("objective", "current_state_and_gap", "leverage", "strategy", "recipient_model")


def _strip_json_fences(text: str) -> str:
    """LLMs frequently wrap JSON in ```json ... ``` fences. Strip them."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return stripped


def parse_compose_result(llm_text: str, shareable_ids: set[str]) -> dict:
    cleaned = _strip_json_fences(llm_text or "")
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return {"error": "llm returned non-json", "draft_text": "", "raw": llm_text}

    if not isinstance(parsed, dict):
        return {"error": "llm returned non-object", "draft_text": "", "raw": llm_text}

    reply_needed = parsed.get("reply_needed")
    if not isinstance(reply_needed, bool):
        return {"error": "missing reply_needed (must be bool)", "draft_text": "", "raw": llm_text}

    missing_reasoning = [
        f for f in _REQUIRED_REASONING_FIELDS
        if not isinstance(parsed.get(f), str) or not parsed.get(f, "").strip()
    ]
    if missing_reasoning:
        return {
            "error": f"missing required reasoning fields: {missing_reasoning}",
            "draft_text": "",
            "raw": llm_text,
        }

    draft = parsed.get("draft_text", "") or ""
    fyi = parsed.get("no_reply_fyi", "") or ""

    if reply_needed:
        if not isinstance(draft, str) or not draft.strip():
            return {"error": "reply_needed=true but draft_text is empty", "draft_text": "", "raw": llm_text}
    else:
        if not isinstance(fyi, str) or not fyi.strip():
            return {"error": "reply_needed=false but no_reply_fyi is empty", "draft_text": "", "raw": llm_text}

    reasoning = parsed.get("reasoning_summary", "")
    if not isinstance(reasoning, str):
        reasoning = ""

    cited = parsed.get("cited_fact_ids", [])
    if not isinstance(cited, list):
        cited = []
    cited_clean = [c for c in cited if isinstance(c, str) and c in shareable_ids]

    return {
        "reply_needed": reply_needed,
        "objective": parsed["objective"].strip(),
        "current_state_and_gap": parsed["current_state_and_gap"].strip(),
        "leverage": parsed["leverage"].strip(),
        "strategy": parsed["strategy"].strip(),
        "recipient_model": parsed["recipient_model"].strip(),
        "draft_text": draft.strip() if isinstance(draft, str) else "",
        "no_reply_fyi": fyi.strip() if isinstance(fyi, str) else "",
        "reasoning_summary": reasoning,
        "cited_fact_ids": cited_clean,
    }
