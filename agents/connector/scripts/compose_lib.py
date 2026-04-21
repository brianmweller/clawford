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
import re
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
  "draft_text":             "<when reply_needed=true: the email reply body, no subject line. The canonical sign-off will be appended post-process, so you don't need to add one — if you naturally close with the sign-off anyway, we'll normalize. Match the voice anchors from history LITERALLY — sentence length, contractions, hedging. When reply_needed=false: empty string.>",
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

  FORMATTING: emit each paragraph as a single unwrapped line separated
  by a blank line. Do NOT hard-wrap prose at any column width — Gmail
  handles rendering. Hard wraps become visible mid-paragraph linebreaks
  and look like the email was pasted from a terminal.

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


# Common sign-off openers the LLM may emit even though we instruct otherwise.
# Order matters only for readability; the regex below uses alternation.
_SIGNOFF_OPENERS = (
    "best", "love", "cheers", "warmly", "thanks", "thank you",
    "sincerely", "regards", "kind regards", "take care", "talk soon",
    "all the best", "many thanks",
)


def _is_signoff_line(line: str, name: str) -> bool:
    """True when this line looks like part of a sign-off.

    Matches:
      - The bare first name on its own line ("the operator")
      - "-- the operator" / "— the operator" dash-prefixed variants
      - Closing words alone ("Best," / "Love," / "Cheers")
      - Closing words followed by the name ("Best, the operator")
    Keeps the matcher narrow so substantive short lines like "Thanks!"
    at the end of a paragraph don't get eaten.
    """
    s = line.strip()
    if not s:
        return False
    low = s.lower()
    name_low = name.lower()
    if low == name_low:
        return True
    if re.fullmatch(rf"[-—–]+\s*{re.escape(name_low)}\.?", low):
        return True
    openers = "|".join(re.escape(o) for o in _SIGNOFF_OPENERS)
    if re.fullmatch(rf"({openers}),?", low):
        return True
    if re.fullmatch(rf"({openers}),?\s+{re.escape(name_low)}\.?", low):
        return True
    return False


def _normalize_signoff(body: str, signoff: str) -> str:
    """Strip any trailing sign-off-like lines and append the canonical signoff.

    The canonical form is whatever the voice profile specifies. Up to 3
    trailing lines are inspected (covers "Best,\\nBrian" plus a wrapped
    dash-line variant); anything further up is left alone.
    """
    if not signoff:
        return body.rstrip()
    # Extract the name token from the signoff: either the whole last line
    # ("the operator") or, for single-line forms ("Love, the operator"), the last
    # alphabetic word.
    last_line = signoff.strip().splitlines()[-1].strip()
    name_tokens = re.findall(r"[A-Za-z][A-Za-z'\-]*", last_line)
    name = name_tokens[-1] if name_tokens else last_line
    lines = body.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    stripped = 0
    while lines and stripped < 3:
        if _is_signoff_line(lines[-1], name):
            lines.pop()
            stripped += 1
            while lines and not lines[-1].strip():
                lines.pop()
        else:
            break
    cleaned = "\n".join(lines).rstrip()
    if not cleaned:
        return signoff
    return f"{cleaned}\n\n{signoff}"


_BULLET_RE = re.compile(r"^\s*([-*•]|\d+[.)])\s")


def _dehardwrap(body: str) -> str:
    """Un-hard-wrap LLM-emitted prose paragraph-by-paragraph.

    LLMs default to wrapping prose at ~68 chars; Gmail renders those as
    visible mid-paragraph linebreaks. Collapse within a paragraph while
    preserving blank-line separators. Paragraphs containing bullet /
    numbered list markers keep their internal line breaks.
    """
    if not body.strip():
        return body
    paragraphs = re.split(r"\n\s*\n", body)
    out = []
    for p in paragraphs:
        lines = [l.rstrip() for l in p.splitlines()]
        if any(_BULLET_RE.match(l) for l in lines if l.strip()):
            out.append("\n".join(l for l in lines))
        else:
            out.append(" ".join(l.strip() for l in lines if l.strip()))
    return "\n\n".join(out)


def apply_post_processing(parsed: dict, voice_profile: dict | None) -> dict:
    """Apply deterministic post-LLM transforms to a parsed compose result.

    Order matters: dehardwrap first (so paragraph shape is canonical),
    then sign-off normalization (operates on the last non-blank line).
    """
    if not isinstance(parsed, dict) or parsed.get("error"):
        return parsed
    if not parsed.get("reply_needed"):
        return parsed
    draft = parsed.get("draft_text", "") or ""
    if not draft.strip():
        return parsed
    draft = _dehardwrap(draft)
    signoff = ((voice_profile or {}).get("profile_signoff") or "").strip()
    if signoff:
        draft = _normalize_signoff(draft, signoff)
    parsed["draft_text"] = draft
    return parsed
