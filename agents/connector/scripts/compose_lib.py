"""Pure helpers for draft-compose.py.

build_compose_prompt: assembles the LLM prompt from a RecipientContext,
voice-guidance bundle, the inbound email, and optional availability slots.
The prompt forces a structured five-step reasoning pass — recipient model,
objective, state/gap, leverage, strategy — BEFORE any prose is written.
Recipient model is step 1 by design: it's the generative seed for every
step that follows, not a post-hoc sanity check. A draft without an
explicit intent is a pleasantry, not a reply; the shape of the output
enforces that discipline.

parse_compose_result: normalizes the JSON the LLM returns, validates the
five reasoning fields are populated, and strips any cited_fact_ids that
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
  "recipient_model":        "<two–three sentences. Step 1 — generative seed for everything below. Answer BOTH: (1) what decision / action are they expecting task-wise? and (2) what EMOTIONAL OUTCOME do they want from the reply — to feel appreciated, useful, heard, forgiven, reassured, etc.? Gift-givers want the gift to feel loved, not tolerated. Advice-givers want acknowledgment the advice landed. Well-wishers want engagement with what they said. Closeout-senders want the thread to end gracefully. If the draft nails the task but misses the emotional transaction, the reply reads as cold. Everything below flows from this model.>",
  "objective":              "<one sentence: what is the operator trying to achieve, framed as serving the recipient_model above? If reply_needed=false, the objective is what the operator gains by NOT replying (preserving the recipient's frame, respecting their closeout, etc.).>",
  "current_state_and_gap":  "<two–three sentences: given the context, where does the operator stand relative to the objective, and what does the recipient need to see / hear / feel to close the gap?>",
  "leverage":               "<two–three sentences: what SPECIFIC assets does the operator have here that ANSWER the recipient's need — named people who can vouch, prior moves already made, concrete shared context, proof points? Enumerate at least one. Select leverage by what serves the recipient's decision, not by what the operator is proud of. If there is genuinely no leverage, say so plainly.>",
  "strategy":               "<two–three sentences: the concrete tactical move. If reply_needed=true, this is what the draft will DO (MUST deploy the leverage in service of the recipient_model). If reply_needed=false, this is why silence is the right move and what it protects.>",
  "draft_text":             "<when reply_needed=true: the email reply body, no subject line. The canonical sign-off will be appended post-process, so you don't need to add one — if you naturally close with the sign-off anyway, we'll normalize. Match the voice anchors from history LITERALLY — sentence length, contractions, hedging. When reply_needed=false: empty string.>",
  "no_reply_fyi":           "<when reply_needed=false: one short sentence the operator will read on Telegram — what arrived, why no reply is needed, any watch-for-later note. When reply_needed=true: empty string.>",
  "reasoning_summary":      "<one sentence anchored in objective + strategy — what the operator reads on Telegram alongside the draft (or alongside no_reply_fyi) to decide whether to ship/override.>",
  "cited_fact_ids":         ["<fact id>", ...]
}
"""


_COLD_INBOUND_SCHEMA_HINT = """\
Respond with a JSON object with EXACTLY these fields, in this order:
{
  "fit_assessment": {
    "tier":         "A | B | C | not_a_target | unclear",
    "domain_fit":   "strong | moderate | weak",
    "level_fit":    "strong | moderate | weak",
    "function_fit": "strong | moderate | weak",
    "target_company_match": "<company name from the operator's target_companies if this IS one, else empty>",
    "rationale":    "<two–three sentences. Name the specific domain / level / function signals that drove the tier. Cite evidence from SELF CONTEXT. Do NOT just say 'company is/isn't in target_companies' — explain the underlying fit.>"
  },
  "reply_needed":           true,
  "recipient_model":        "<two–three sentences. Step 1 — generative seed for everything below. (1) TASK: the recruiter needs a clean yes/no on whether the operator wants the call + concrete availability. (2) FILTER SIGNAL: she wants to know what the operator is responding to so she can decide how to use the 30 minutes — keep selling, reframe, or qualify out. She does NOT need a fit-pitch in the email; she's already made her fit hypothesis. The call is where fit gets demonstrated.>",
  "objective":              "<for A/B: 'get the call on the calendar and surface honest filter signal'. The email's job is to SCHEDULE, not to pitch fit — Michelle already did the fit-pattern-matching or she wouldn't be reaching out. For decline-fit: 'preserve relationship'. For unclear: 'get one clarifying signal'.>",
  "current_state_and_gap":  "<two–three sentences. The recruiter has the operator's LinkedIn (background) and has already made a fit hypothesis. What she does NOT know is his current status, what would make him prioritize this over other options, and whether to schedule. The reply closes THAT gap — not a fit-pitch gap.>",
  "compelling_angle":       "<one phrase (not a sentence) — the honest 'why this role, why listening now' hook. This is FILTER signal for the recruiter, not a pitch: it tells her what the operator is responding to and what he's gated on, so she can decide whether to keep selling or reframe. If the operator is late-stage elsewhere, say what specifically about this shape pulls him anyway. No boilerplate enthusiasm. Example: 'the community-flywheel shape isn't a problem shape I see often' — not 'excited about the opportunity'. For decline-fit, leave empty.>",
  "leverage":               "<two–three sentences. For A/B: the ask is already warm — Michelle pitched specifically, not blasted. The leverage is responding in kind: engage with ONE element of her pitch + surface the compelling_angle + propose times. Do NOT pitch fit back at her — she did the fit-matching work. For decline-fit: what preserves the relationship without brand criticism.>",
  "strategy":               "<two–three sentences. For A/B (interesting): TAKE THE CALL — propose specific times; weave compelling_angle into the draft as honest filter signal. Do NOT load the email with scope questions. Do NOT pitch the operator's credentials / fit — the recruiter already did that work; restating it inverts the power dynamic (the operator is being pitched, not auditioning). For decline-fit: polite one-line decline citing scope/timing not brand. For unclear: ONE specific clarifying question, nothing more.>",
  "draft_text":             "<3-5 sentences for A/B. MUST carry: (1) engagement with ONE specific element of their pitch (not three — LLM-parallelism tell), (2) compelling_angle hook — honest 'why listening now' woven in as filter signal (one phrase), (3) concrete availability (two time windows). Do NOT include a fit-pitch / credentials restatement — that belongs on the call. Three beats: engage, angle, schedule. Short. No monologue. Match the operator's voice (from recruiter voice profile). No corporate hedge phrases.>",
  "no_reply_fyi":           "",
  "reasoning_summary":      "<one sentence leading with tier + fit dimensions: 'B-tier / Reddit Sr Dir / strong domain+level+function / took the call'>",
  "cited_fact_ids":         ["<fact id>", ...]
}
"""


def _render_self_context_block(self_profile: dict) -> str:
    """Render the operator's professional brain as a prompt section. Called
    only for cold recruiter inbounds — known senders don't need this
    context (Huckle already has per-recipient facts and voice anchor).

    self_profile is a dict with: profile_md (str), level_bar_text (str),
    current_targets (list), strength_themes (list), employer_history
    (list). Keys missing → that subsection is skipped.
    """
    from datetime import date as _date

    parts: list[str] = []

    profile_md = self_profile.get("profile_md") or ""
    level_bar = self_profile.get("level_bar_text") or ""
    current_targets = self_profile.get("current_targets") or []
    strengths = self_profile.get("strength_themes") or []
    employers = self_profile.get("employer_history") or []

    parts.append("SELF CONTEXT (the operator's professional brain — you are drafting on")
    parts.append("his behalf; use this to calibrate fit + positioning + voice):")
    parts.append("")

    if employers:
        # Determine the operator's CURRENT status: currently employed vs
        # between-roles / active-search. The most recent employer's
        # end date drives this — if end is in the past, the operator is NOT
        # at that company anymore.
        today_iso = _date.today().isoformat()
        latest = max(employers, key=lambda e: e.get("end", ""))
        latest_end = (latest.get("end") or "").strip()
        is_currently_employed = bool(latest_end) and latest_end > today_iso

        if is_currently_employed:
            parts.append("  CURRENT STATUS: currently employed")
            parts.append(f"    {latest.get('title', '?')} at {latest.get('company', '?')} "
                         f"({latest.get('start', '?')} → {latest.get('end', '?')})")
        else:
            parts.append("  CURRENT STATUS: post-employment / active executive search")
            parts.append(f"    Most recent role (ended {latest_end or 'unknown'}): "
                         f"{latest.get('title', '?')} at {latest.get('company', '?')}")
            parts.append(f"    Today's date: {today_iso} — the operator is NOT currently at "
                         f"{latest.get('company', '?')}. Recruiters who reached out "
                         f"recently know this from LinkedIn.")
        parts.append("")

    if level_bar:
        parts.append("  LEVEL & SCOPE BAR (hard filter the operator applies to every role):")
        for line in level_bar.splitlines():
            parts.append(f"    {line}")
        parts.append("")

    if current_targets:
        parts.append("  CURRENT TARGET COMPANIES (by opportunity tier):")
        # Group by tier_opportunity
        by_tier: dict[str, list[dict]] = {}
        for t in current_targets:
            key = t.get("tier_opportunity") or "?"
            by_tier.setdefault(key, []).append(t)
        for tier in ["A", "B", "C"]:
            if tier not in by_tier:
                continue
            parts.append(f"    {tier}-opportunity:")
            for t in by_tier[tier]:
                role = t.get("role_type") or "(role_type not specified)"
                tier_co = t.get("tier_company", "?")
                parts.append(f"      - {t.get('company', '?')} "
                             f"[tier_company={tier_co}] — {role}")
        parts.append("")

    if strengths:
        parts.append("  STRENGTH THEMES (what the operator can credibly claim):")
        for s in strengths[:6]:
            parts.append(f"    - {s.get('theme', '?')}")
        parts.append("")

    # Active search pipeline — load-bearing urgency signal for drafting.
    # If the operator is in late stages with other opportunities, the reply
    # stance shifts: he can be slightly more assertive about needing a
    # near-term meeting, can truthfully say he's actively interviewing,
    # and the recruiter will know he's not at idle capacity.
    pipeline = self_profile.get("active_search_pipeline") or []
    late_stage = self_profile.get("late_stage_searches") or []
    if pipeline:
        parts.append("  CURRENT ACTIVE SEARCH PIPELINE (load-bearing urgency + calendar context):")
        for s in pipeline[:10]:
            notes = (s.get("notes") or "").strip()
            notes_tail = f" — {notes[:80]}" if notes else ""
            parts.append(f"    - [{s.get('stage', '?'):20s}] {s.get('company', '?'):20s} "
                         f"last signal {s.get('last_signal_date', '')}{notes_tail}")
        if late_stage:
            parts.append(f"    LATE-STAGE ENGAGEMENTS: {len(late_stage)} active "
                         f"({', '.join(s.get('company', '?') for s in late_stage[:5])}).")
            parts.append(f"    USE THIS: when late-stage engagements exist, the draft "
                         f"MUST include a brief urgency line near the time proposal — "
                         f"e.g., 'in late stages with a few other processes, so sooner "
                         f"would be better if you can' or 'juggling a couple of active "
                         f"interview loops, so any time this week would help.' This "
                         f"matches how the operator actually replies — he names real constraints. "
                         f"Do NOT name specific competitor companies. Do NOT omit the "
                         f"signal — omitting it understates how hot the operator's market is.")
        parts.append("")

    if profile_md:
        # Only include the profile's Self-description + top Track record
        # to keep prompt bounded. Full profile.md is too large.
        excerpt = _excerpt_profile_md(profile_md, max_chars=1200)
        parts.append("  PROFILE EXCERPT (voice anchor):")
        for line in excerpt.splitlines():
            parts.append(f"    {line}")
        parts.append("")

    return "\n".join(parts)


def _render_company_context_block(company_brief: dict) -> str:
    """Render the COMPANY CONTEXT block from a company_research.CompanyBrief's
    to_prompt_dict(). Called only for cold-inbound drafts — known-sender
    drafts have per-recipient facts and voice anchors that carry the
    same load.

    The block sits between SELF CONTEXT and FIT CHECK so by the time the
    LLM reaches STRATEGY, the hooks_to_drop and operator_fit guidance
    are fresh. Positioning is load-bearing: drafts tail-weighted toward
    fit-check instructions tend to pull the most specific hooks the
    model has seen, which is exactly what we want.
    """
    company_name = company_brief.get("company_name", "")
    what_they_do = company_brief.get("what_they_do", "")
    stage = company_brief.get("stage_signal", "unknown")
    confidence = company_brief.get("confidence", "low")
    recent_news = company_brief.get("recent_news") or []
    role_ctx = company_brief.get("role_context") or {}
    operator_fit = company_brief.get("operator_fit") or {}

    parts: list[str] = []
    parts.append(
        f"COMPANY CONTEXT (external research on {company_name} — facts the "
        f"recruiter assumes the operator already knows or will look up; use them "
        f"to tailor the draft away from generic and show the operator read the "
        f"room):"
    )
    parts.append("")
    parts.append(f"  COMPANY:       {company_name}")
    if what_they_do:
        parts.append(f"  WHAT THEY DO:  {what_they_do}")
    parts.append(f"  STAGE:         {stage} (research confidence: {confidence})")

    if recent_news:
        parts.append("")
        parts.append("  RECENT NEWS:")
        for item in recent_news[:5]:
            bullet = (item.get("bullet") or "").strip() if isinstance(item, dict) else str(item)
            dated = (item.get("dated") or "").strip() if isinstance(item, dict) else ""
            if dated:
                parts.append(f"    - [{dated}] {bullet}")
            else:
                parts.append(f"    - {bullet}")

    if role_ctx:
        parts.append("")
        parts.append("  ROLE CONTEXT (from JD signal):")
        if role_ctx.get("title"):
            parts.append(f"    Title:            {role_ctx['title']}")
        if role_ctx.get("team_hint"):
            parts.append(f"    Team:             {role_ctx['team_hint']}")
        if role_ctx.get("level_band_hint"):
            parts.append(f"    Level band:       {role_ctx['level_band_hint']}")
        if role_ctx.get("scope_hint"):
            parts.append(f"    Scope:            {role_ctx['scope_hint']}")

    angles = operator_fit.get("strength_angles") or []
    concerns = operator_fit.get("concerns") or []
    hooks = operator_fit.get("hooks_to_drop") or []
    qs = operator_fit.get("questions_to_ask") or []

    if angles or concerns:
        parts.append("")
        parts.append("  FIT ANALYSIS (tailored to the operator's strength themes + level bar):")
        if angles:
            parts.append("    Strength angles:")
            for a in angles[:5]:
                parts.append(f"      - {a}")
        if concerns:
            parts.append("    Concerns (be honest about these in the reply, don't paper over):")
            for c in concerns[:5]:
                parts.append(f"      - {c}")

    if hooks:
        parts.append("")
        parts.append(
            "  HOOKS TO DROP (one-two phrases that prove the operator read about "
            "them — weave ONE into the draft as the COMPELLING ANGLE or as "
            "the ENGAGEMENT element with their pitch):"
        )
        for h in hooks[:4]:
            parts.append(f"    - {h}")

    if qs:
        parts.append("")
        parts.append(
            "  QUESTIONS THE RESEARCH SUGGESTS (a starting point — refine or "
            "replace when you can do better. PREFER these over generic "
            "'tell me about the team structure' fabrications. Remember: "
            "per POWER DYNAMIC below, most questions belong on the CALL "
            "not in the reply — surface at most ONE in the email if it's "
            "the load-bearing dimension the reply needs to gate on):"
        )
        for q in qs[:5]:
            parts.append(f"    - {q}")

    return "\n".join(parts)


def _excerpt_profile_md(profile_md: str, max_chars: int = 1200) -> str:
    """Pull the Self-description section from profile.md, capped. For
    prompt size control; the full profile is too large to inject."""
    if not profile_md:
        return ""
    # Find ## Self-description
    m = re.search(r"^##\s+Self-description\s*$", profile_md, flags=re.MULTILINE)
    if not m:
        return profile_md[:max_chars]
    start = m.end()
    # Stop at next ##
    next_m = re.search(r"^##\s+", profile_md[start:], flags=re.MULTILINE)
    end = start + next_m.start() if next_m else len(profile_md)
    return profile_md[start:end].strip()[:max_chars]


def build_compose_prompt(
    context: RecipientContext,
    voice: dict,
    inbound: dict,
    availability_slots: list[tuple[datetime, datetime]] | None = None,
    *,
    cold_inbound: bool = False,
    self_profile: dict | None = None,
    operator_hint: str | None = None,
    company_brief: dict | None = None,
) -> str:
    person = context.recipient_person
    name = person.get("full_name") or person.get("slug", "them")

    facts_lines = []
    for f in context.facts_shareable:
        # Theory-of-mind tag: facts the recipient has already been told
        # get a leading marker so the LLM doesn't re-present them as new
        # information. The context_builder sets recipient_knows based on
        # whether the recipient's slug is in the fact's known_by list.
        known_tag = " [RECIPIENT KNOWS]" if f.get("recipient_knows") else ""
        facts_lines.append(f"  - [{f['id']}]{known_tag} {f['content']}")
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

    recipient_tz_str = person.get("timezone") or ""
    recipient_tz = None
    if recipient_tz_str:
        try:
            from zoneinfo import ZoneInfo
            recipient_tz = ZoneInfo(recipient_tz_str)
        except Exception:  # noqa: BLE001 - bad tz string → skip dual render
            recipient_tz = None

    slots_section = ""
    if availability_slots:
        slot_lines = []
        for s, e in availability_slots:
            base = f"  - {s.strftime('%a %b %d %H:%M')}–{e.strftime('%H:%M %Z')}"
            # Dual-tz render when recipient tz differs from slot tz.
            if recipient_tz is not None and str(recipient_tz) != str(s.tzinfo):
                rs = s.astimezone(recipient_tz)
                re_ = e.astimezone(recipient_tz)
                base += f"  ({rs.strftime('%H:%M')}–{re_.strftime('%H:%M %Z')} their time)"
            slot_lines.append(base)
        slots_section = "\nOPEN SLOTS (propose these if the sender asked to schedule):\n" + "\n".join(slot_lines) + "\n"

    profile_section = ""
    if voice.get("profile_present"):
        patterns = "\n".join(f"    - {p}" for p in voice.get("profile_patterns", [])) or "    (none)"
        antis = "\n".join(f"    - {a}" for a in voice.get("profile_anti_patterns", [])) or "    (none)"
        openings = ", ".join(f'"{o}"' for o in voice.get("profile_opening_phrases", [])) or "(none)"
        thread_position = voice.get("thread_position", "replying")
        profile_section = (
            "\nLEARNED VOICE PROFILE (distilled from the operator's prior sent mail to people\n"
            "in this circle — these patterns OVERRIDE abstract register guidance when\n"
            "they disagree)\n"
            f"  Typical greeting:   {voice.get('profile_greeting')}\n"
            f"  Typical signoff:    (appended post-process — do not emit)\n"
            f"  Common patterns:\n{patterns}\n"
            f"  Anti-patterns (avoid these):\n{antis}\n"
            f"  Opening phrases to match: {openings}\n"
            f"  Distinctive trait:  {voice.get('profile_distinctive_traits')}\n"
            "\n"
            f"  OPENER CHOICE (thread_position={thread_position}): the typical\n"
            "  greeting and any 'Thanks, {name}.' / 'Thank you, {name}.' reply\n"
            "  opener in Common patterns are ALTERNATIVES — use ONE, never both\n"
            "  in the same message. Choose by thread_position:\n"
            "    - originating / reopening: lead with the typical greeting\n"
            "      ('Hi Jamie,' style) and proceed into substance.\n"
            "    - replying / following_up: lead with the reply-opener\n"
            "      ('Thanks, Jamie.' / 'Thank you, Jamie.') as its own line,\n"
            "      then proceed into substance. Do NOT also open with the\n"
            "      typical greeting — that stacks the name twice in quick\n"
            "      succession and reads as generated.\n"
        )

    # Operator hint block — when the operator manually invokes /reply with a
    # hint ("make it warmer", "he already accepted, mention that I'm
    # pausing the search", "2 sentences shorter"), the hint is appended
    # to the USER prompt at the top so it takes precedence over the
    # voice / history / brain defaults. The hint may carry either
    # stylistic guidance OR factual context the brain doesn't have yet;
    # either way, treat it as authoritative ground truth for this draft.
    operator_hint_block = ""
    if operator_hint and operator_hint.strip():
        operator_hint_block = (
            "OPERATOR HINT (load-bearing — applies to THIS draft only, "
            "overrides conflicting defaults from voice / history / brain):\n"
            f"  {operator_hint.strip()}\n\n"
            "If the hint adds factual context (dates, decisions, status "
            "updates), treat it as more recent than anything in the brain "
            "and weave it into the draft accordingly. If the hint is "
            "stylistic (shorter, warmer, less formal), apply it to the "
            "final draft_text AND to the reasoning that produces it — "
            "don't just append a cosmetic pass at the end.\n\n"
        )

    return f"""{operator_hint_block}You are drafting an email reply on the operator's behalf. Do NOT send it — the operator will review.

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


  1. RECIPIENT MODEL (theory of mind) — START HERE. This is the
     generative seed for every step that follows, not a post-hoc sanity
     check. Who is reading this? What are they deciding? What are they
     already worried about? What's the one thing they need to see,
     hear, or feel to lean in?

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

     Everything downstream — the objective you choose, the leverage you
     select, the strategy you execute — should FOLLOW FROM this model,
     not validate against it. Sender-frame drafting (starting from "what
     does the operator want?" and bolting on a recipient check at the end) is
     the exact failure mode this ordering exists to prevent.
  2. OBJECTIVE / INTENT — what is the operator trying to achieve with
     this communication, framed as serving the recipient model above? Be
     specific and outcome-oriented. Not "reply warmly"; rather "keep the
     candidacy pipeline alive for future roles at this company" or
     "decline without burning the bridge" or "lock in a meeting this
     week to unblock X."
  3. CURRENT STATE AND GAP — given the brain context, email history, and
     inbound message: where does the operator stand relative to the objective,
     and what does the RECIPIENT need to see / hear / feel to close the
     gap?
  4. LEVERAGE / ASSETS — what SPECIFIC assets does the operator have here that
     ANSWER the recipient's need? Named people who can vouch. Prior
     moves already made in this thread. Proof points. Shared context
     that's load-bearing. Enumerate at least one concrete item. Select
     leverage by what serves the recipient's decision, not by what the operator
     is proud of. If there is genuinely no leverage, say so plainly —
     that usually means the right move is restraint, not more words.
  5. STRATEGY — the concrete moves the draft will make, IN SERVICE of
     the recipient model. MUST explicitly deploy the leverage. What to
     name specifically, what to offer, what kind of ask to surface (if
     any), what to leave unsaid. "Rooting from the outside" is not a
     strategy — it accomplishes nothing. Every sentence in the draft
     must serve a concrete move that lands the recipient's emotional +
     task outcomes.

Only after you've worked through all five should you decide reply_needed
and, if true, draft. If false, populate no_reply_fyi with a one-line
summary for the operator's Telegram ping instead.

RECIPIENT
  Name: {name}
  Relationship: {person.get("relationship_type", "unknown")}
  Slug: {person.get("slug", "")}{(chr(10) + "  Time zone: " + recipient_tz_str) if recipient_tz_str else ""}

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

Facts tagged [RECIPIENT KNOWS] are things the recipient has already been told
— either they were on the original email thread the fact came from, or they
attended the meeting it was observed in. Do NOT present these as new
information. You may still cite them sparingly when the reply's logic
requires acknowledging shared context ("as I mentioned Tuesday..."), but
the default is to leave them out. Facts without the tag are genuinely
new-to-the-recipient — those are the ones worth surfacing when they advance
the objective.
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

{_render_self_context_block(self_profile) if cold_inbound and self_profile else ""}

{_render_company_context_block(company_brief) if cold_inbound and company_brief else ""}

{(
    "FIRST PRINCIPLE — BEFORE anything else, apply the recipient-"
    "perspective frame:\n\n"
    "  The recruiter invested time researching the operator and writing this "
    "outreach. Every reply — engage, decline, or clarify — should leave "
    "them feeling their pitch was actually read, considered, and treated "
    "with respect. A brusque, procedural, or hedging reply reflects "
    "badly on the operator regardless of whether the role is right.\n\n"
    "  Before emitting the draft, ask yourself: WOULD THE RECRUITER "
    "FEEL RESPECTED BY THIS REPLY? If the answer is 'maybe' or 'no,' "
    "rewrite. Respect looks like: acknowledging a specific element of "
    "their pitch (the role, the hiring manager they named, the problem "
    "they described), responding to what they actually wrote rather "
    "than boilerplate, and being honest about fit in a way that gives "
    "them useful information. It does NOT require flattery or length — "
    "short and substantive beats long and hedgy.\n\n"
    "  Decline is always fine IF done respectfully. 'Not the right fit "
    "right now, but appreciate you thinking of me' >> ghosting or "
    "generic declines.\n\n"
    "FIT CHECK (cold recruiter inbound — step 0.5, before the reasoning "
    "steps below):\n\n"
    "the operator evaluates opportunities on a COMPOSITE of three primary "
    "dimensions, plus target_company as a supporting signal. Score each "
    "dimension strong / moderate / weak based on evidence in the inbound "
    "message vs SELF CONTEXT:\n\n"
    "  1. DOMAIN FIT — does the company's business resonate with the operator's "
    "background?\n"
    "     Strong: marketplaces, two-sided platforms, content platforms, "
    "consumer/community businesses, pricing, ads monetization, search/"
    "ranking, personalization.\n"
    "     Moderate: adjacent consumer/enterprise surfaces where the operator's "
    "methods (causal ML, experimentation, org design) transfer.\n"
    "     Weak: industries where the operator has no prior exposure (deep-tech "
    "hardware, heavily regulated, pure B2B without consumer/marketplace "
    "exposure).\n\n"
    "  2. LEVEL FIT — passes the operator's level & scope bar?\n"
    "     Strong: owns a major lever of company success (pricing, supply, "
    "ranking, monetization, growth) OR directly C-suite-adjacent "
    "(reports to CEO/CTO/CDO/EVP, or is a Head-of-function).\n"
    "     Moderate: Senior Director reporting to EVP/SVP where scope is "
    "unclear; may own a lever but hard to tell from inbound.\n"
    "     Weak: Director several layers from exec; narrow scope; IC-only.\n\n"
    "  3. FUNCTION FIT — does the role draw on the operator's strengths?\n"
    "     Strong: applied ML + causal inference + economic modeling + "
    "experimentation + marketplace systems thinking + org design.\n"
    "     Moderate: some overlap — pure ML engineering, product analytics, "
    "research leadership.\n"
    "     Weak: research-only, pure engineering management, sales/GTM, "
    "finance/ops.\n\n"
    "  4. TARGET_COMPANY — is the company in the operator's explicit targets? "
    "If yes, that's a strong supporting signal (elevates tier by one "
    "step). If no, NOT disqualifying — interesting opportunities surface "
    "outside the list all the time.\n\n"
    "COMPOSITE TIER from dimensions:\n"
    "  A: two or three dimensions STRONG, level not weak, AND (in "
    "target_company OR frontier-AI lab OR strong-DS-brand OR unique "
    "role scope worth pursuing).\n"
    "  B: two dimensions strong OR one strong + one moderate + plausible "
    "level fit. Reddit's Senior-Director-reporting-to-EVP with "
    "economic-modeling-for-community-flywheel scope is B — strong "
    "domain + moderate level + strong function.\n"
    "  C: only one dimension strong and level weak; low-scale impact.\n"
    "  not_a_target: all dimensions weak or level clearly below bar.\n"
    "  unclear: not enough info in the inbound to score two of the three "
    "dimensions.\n\n"
    "STRATEGY by tier — THIS IS LOAD-BEARING, READ CAREFULLY:\n\n"
    "  A or B (interesting): TAKE THE CALL. Acknowledge briefly, signal "
    "which dimensions resonate (one phrase each, not an essay), then "
    "propose specific times for an intro call. The CALL IS THE SCREEN — "
    "scope questions belong there, not in the email. Do NOT ask for "
    "reporting line / headcount / ownership / scope / mandate in the "
    "email; those are conversational inputs. A 3-5 sentence note with "
    "two or three concrete availability windows is the target shape.\n\n"
    "  C or not_a_target: polite one-line decline that preserves the "
    "relationship. Cite scope or timing, NOT brand. Don't criticize the "
    "company or question their DS org. 2-3 sentences.\n\n"
    "  unclear: one specific clarifying question about the SINGLE "
    "load-bearing dimension you can't score (usually level). Not a wall "
    "of questions. Example: 'Happy to learn more — can you share the "
    "reporting line and team size?' Then propose time contingent on the "
    "answer.\n\n"
    "OPENER STRATEGY (load-bearing — the recruiter has already seen "
    "the operator's LinkedIn and is reaching out with a specific pitch in mind):\n\n"
    "  The LEVERAGE in a recruiter-reply is NOT rehashing your own "
    "credentials. The recruiter already has them — they researched the operator "
    "before reaching out. Leading with 'I lead X at Y' or 'My background "
    "is in Z' is redundant at best, self-centered at worst. It reads as "
    "'I didn't read your note carefully; here's my resume.'\n\n"
    "  INSTEAD: the opener should engage with THEIR pitch. What specific "
    "element of what they wrote is the operator responding to? Name ONE OR TWO "
    "things (not three), conversationally. Show the operator read the note.\n\n"
    "  Credentials come LATER (if at all) as confirmation of fit, not "
    "the lead. Often a single phrase is enough. Sometimes omit entirely "
    "— the recruiter has LinkedIn.\n\n"
    "  OBJECTIVE / STRATEGY reasoning should name the knowledge gap: "
    "the recruiter knows the operator's background but NOT what he's optimizing "
    "for next, or his current status. The reply's leverage is showing "
    "the operator engaged with THEIR pitch and is serious about this specific "
    "role.\n\n"
    "POWER DYNAMIC — THE EMAIL IS SCHEDULING, NOT PITCHING:\n\n"
    "  The recruiter is reaching out to the operator, not the other way around. "
    "She has already read his LinkedIn, already made a fit hypothesis, "
    "and is pitching HIM on the role. That matters for the voice:\n\n"
    "    - the operator is being courted, not auditioning. A reply that pitches "
    "fit inverts that — it reads as low-status and signals over-eagerness. "
    "It also pre-commits the operator to a framing before he's heard the role "
    "details, narrowing his own scope pre-screen.\n"
    "    - The recruiter does NOT need advocate-ready evidence from the "
    "email. She gets that from the 30-minute call. The email gets the "
    "call on the calendar.\n"
    "    - Therefore: DO NOT include a fit-pitch / credentials "
    "restatement / 'here's why I'm a match' line. The recruiter has "
    "LinkedIn; pitching fit back at her is redundant and low-status.\n\n"
    "MUST-CARRY CONTENT (the email has ONE job — get the call on the "
    "calendar — but carries one load-bearing filter signal alongside):\n\n"
    "  The email's job is SCHEDULE + FILTER. The CALL is where fit gets "
    "demonstrated and evaluated. Two different media, two different roles.\n\n"
    "  The draft carries THREE beats:\n\n"
    "    1. ENGAGEMENT — one specific element of her pitch the operator is "
    "responding to. Not three (LLM-parallelism tell). He's reacting to "
    "what she wrote, not spotting it — frame it that way ('the X piece "
    "of this,' 'the Y scope,' 'what you described about Z') rather than "
    "as discovery ('caught my eye,' 'jumped out,' 'stood out'). Show "
    "the operator read the note.\n\n"
    "    2. COMPELLING ANGLE — one honest 'why listening now' phrase. "
    "This is FILTER signal for the recruiter, not a pitch: it tells her "
    "what the operator is responding to so she can decide whether to keep "
    "selling, reframe, or qualify him out. If the operator is late-stage with "
    "other companies (check SELF CONTEXT ACTIVE PIPELINE), the angle "
    "should acknowledge that reality honestly. Avoid boilerplate "
    "('excited about the opportunity,' 'intrigued'). Example: 'the "
    "community-flywheel shape isn't a problem shape I see often' or "
    "'AI safety at frontier-lab scale is a natural extension of where "
    "I've been pointed.'\n\n"
    "    3. CONCRETE AVAILABILITY — two specific time windows. The "
    "scheduling move is the whole point.\n\n"
    "  Three beats, not three paragraphs. Fit demonstration happens on "
    "the CALL — do NOT sneak a fit-pitch in as a fourth beat.\n\n"
    "DO NOT RESTATE INFORMATION THE RECRUITER ALREADY PROVIDED:\n"
    "  - They told you the role title (Senior Director / Head of X / "
    "VP). Don't write 'the Senior Director role' in your reply — they "
    "know what role they're recruiting for. Refer to it as 'this role' "
    "or 'the role' or just the specific scope element.\n"
    "  - Don't restate the company name as context ('your team at Reddit'). "
    "They know where they work.\n"
    "  - Don't restate the hiring manager's name as a title dump ('your "
    "EVP of Ads Monetization Roelof van Zwol') — just 'Roelof' is fine.\n"
    "  - Don't restate the team name verbatim ('Core and Consumer Data "
    "Science org') — reference the work, not the org chart.\n"
    "  - Rule of thumb: if they wrote it, don't echo it back unless "
    "you're responding to a specific phrase from their note.\n\n"
    "WRITE LIKE A HUMAN TALKING, NOT A MEMO:\n"
    "  Compare:\n"
    "    BAD (nominalized, abstract, LLM-speak):\n"
    "      'The combination of economic modeling, community growth, and "
    "Roelof's remit is a compelling match with the kind of work I have "
    "spent much of my time on.'\n"
    "    GOOD (conversational, concrete, human):\n"
    "      'Economic modeling for community growth is right in my "
    "wheelhouse.'\n"
    "      OR: 'This is the kind of problem I love — economic modeling "
    "meeting community flywheels.'\n"
    "      OR: 'Roelof's remit sits right at the intersection of my "
    "background and what I'm looking for next.'\n\n"
    "  Heuristics to sound human:\n"
    "    - Prefer contractions (I'd, I'm, it's, that's, don't) over the "
    "full forms. 'I'd welcome a conversation' > 'I would welcome the "
    "chance to have a conversation.'\n"
    "    - Prefer short concrete phrases over abstract noun-phrase "
    "constructions. 'in my wheelhouse' > 'is a compelling match with.'\n"
    "    - If you're naming 3 things from their pitch, cut to 2 or 1. "
    "Lists of 3 read as LLM-generated parallelism; humans pick the "
    "single most specific thing and go deep.\n"
    "    - No 'the kind of work I have spent much of my time on' — that's "
    "just 'my wheelhouse' or 'my background' in 10 words.\n"
    "    - 'compelling match' / 'directionally aligned' / 'strong fit' "
    "are corporate LLM phrases. Humans say 'right up my alley,' 'in my "
    "wheelhouse,' 'exactly what I've been looking at,' or just describe "
    "the specific thing.\n\n"
    "  BEFORE EMITTING THE DRAFT, read it aloud. If it doesn't sound "
    "like how the operator would talk to a friend about the same email, rewrite.\n\n"
    "VOICE ANTI-PATTERNS (things the operator does NOT say in recruiter replies):\n"
    "  - 'I lead X at Y' as an OPENER in reply context (redundant with "
    "LinkedIn; use only for FIRST-CONTACT outreach the operator initiates).\n"
    "  - 'My background is in X, Y, Z' as an OPENER — same redundancy.\n"
    "  - 'directionally interesting' — corporate hedge; reads as "
    "distance when the reply is actually warm.\n"
    "  - 'This looks/sounds interesting' — weak opening; name the "
    "specific thing in her pitch the operator is responding to.\n"
    "  - 'caught my eye' / 'caught my attention' / 'jumped out' / "
    "'stood out' — discovery-frame idioms. They imply the operator was "
    "browsing / scouting when in fact the recruiter reached out to HIM. "
    "The idiom inverts the power dynamic and reads as corporate-speak. "
    "Instead, name the thing directly: 'the greenfield piece of this,' "
    "'CTO-level scope,' 'the X angle is what I'd want to dig into.'\n"
    "  - Walls of scope questions before committing to a call — save "
    "for the call.\n"
    "  - Over-qualifying before a call ('I'd want to understand X, Y, "
    "and Z before I commit') — if interesting, just take the call.\n"
    "  - Present-tense references to past employers. If the operator's most "
    "recent role ENDED (check SELF CONTEXT.CURRENT STATUS), don't write "
    "'I lead X at Y' — write 'I most recently led X at Y' or just refer "
    "to the work without present-tense.\n"
    "  - 'in good faith,' 'in the weeds,' 'at the end of the day' — "
    "business clichés.\n\n"
    "VOICE PATTERNS TO KEEP (from the recruiter voice profile):\n"
    "  - Warm-but-restrained phrasings: 'would welcome the chance to "
    "connect,' 'would be glad to compare notes,' 'happy to find time.'\n"
    "  - Em dash for qualification / aside: 'Head of X reporting to "
    "the CTO — the kind of scope I've been looking at.'\n"
    "  - When declining: 'isn't quite the right fit for me right now' "
    "rather than blunt 'no.'\n\n"
) if cold_inbound else ""}

{(_COLD_INBOUND_SCHEMA_HINT if cold_inbound else _OUTPUT_SCHEMA_HINT)}"""


_REQUIRED_REASONING_FIELDS = ("recipient_model", "objective", "current_state_and_gap", "leverage", "strategy")


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

    out = {
        "reply_needed": reply_needed,
        "recipient_model": parsed["recipient_model"].strip(),
        "objective": parsed["objective"].strip(),
        "current_state_and_gap": parsed["current_state_and_gap"].strip(),
        "leverage": parsed["leverage"].strip(),
        "strategy": parsed["strategy"].strip(),
        "draft_text": draft.strip() if isinstance(draft, str) else "",
        "no_reply_fyi": fyi.strip() if isinstance(fyi, str) else "",
        "reasoning_summary": reasoning,
        "cited_fact_ids": cited_clean,
    }

    # Cold-inbound path: preserve fit_assessment if the LLM returned it.
    fit = parsed.get("fit_assessment")
    if isinstance(fit, dict):
        out["fit_assessment"] = {
            "tier": str(fit.get("tier", "unclear")),
            "rationale": str(fit.get("rationale", "")),
            "matched_target": str(fit.get("matched_target", "")),
        }

    # Cold-inbound path: preserve compelling_angle — the honest "why
    # listening now" filter signal the reply carries. Email's job is
    # schedule + filter, not pitch, so fit_signal is deliberately NOT
    # part of the schema (that's the call's job). Missing or empty is
    # acceptable for decline-fit / unclear tiers.
    compelling_angle = parsed.get("compelling_angle")
    if isinstance(compelling_angle, str):
        out["compelling_angle"] = compelling_angle.strip()

    return out


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
