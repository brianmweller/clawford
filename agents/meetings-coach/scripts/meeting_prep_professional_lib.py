"""meeting_prep_professional_lib — recruiter/hiring-meeting helpers.

Murphy's meeting-prep.py today is pure-template I/O: assemble attendee
facts + commitments + Workflowy agenda, render a templated brief.
That shape is wrong for recruiter calls — the operator needs 3 questions to
ask, 3 talking points to surface, and any red flags grounded in his
*own* self-profile + the specific role the recruiter is pitching.

This module adds two pieces:

1. classify_meeting_type(event, attendees_resolved, self_profile) —
   returns 'recruiter-screen' / 'hiring-manager' / 'hiring-panel' /
   'general' by running a short heuristic ladder over attendee
   relationship_type, ATS domain matches, target_company matches,
   and event title keywords.

2. build_llm_prep(event, attendees_resolved, self_profile, meeting_type) —
   single LLM call (via agents/shared/llm.py) that produces
   {questions_to_ask, talking_points, red_flags, prep_summary}
   grounded in the self-profile excerpts for the matched role.

Degrades open throughout: if self_profile.has_profile is False, the
classifier returns 'general' and build_llm_prep is never called. If
the LLM call fails, build_llm_prep returns {'error': ...} and the
calling renderer falls through to the generic template.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.llm import infer  # noqa: E402
from agents.shared.recruiter_domains import (  # noqa: E402
    AMBIGUOUS_DOMAINS,
    RECRUITER_DOMAINS,
    is_recruiter_domain,
)
from agents.shared.self_profile import SelfProfile  # noqa: E402


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


# Hiring-process title keywords — presence bumps a 'general' classification
# to 'recruiter-screen' when at least one attendee is a known contact.
_HIRING_TITLE_KEYWORDS = {
    "phone screen",
    "intro call",
    "introductory call",
    "recruiter call",
    "recruiter chat",
    "interview",
    "onsite",
    "hiring manager",
    "hm interview",
    "hm chat",
    "hm call",
}


# Hiring-manager-specific keywords — when present AND the attendee matches
# a target company, classify as 'hiring-manager' rather than 'hiring-panel'.
_HIRING_MANAGER_KEYWORDS = {
    "hiring manager",
    "manager chat",
    "manager 1:1",
    "manager 1on1",
    "hm interview",
    "hm chat",
    "hm call",
    "hm 1:1",
    "1:1 with",
}


def _domain_of(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.partition("@")[2].lower().strip()


def _domain_stem(domain: str) -> str:
    """Strip the TLD and subdomain prefix to leave the company stem:
    `hire.lever.co` → `lever`, `alice@google.com` → `google`."""
    if not domain:
        return ""
    parts = domain.split(".")
    if len(parts) < 2:
        return parts[0] if parts else ""
    # For 'mail.notion.so' → ['mail', 'notion', 'so'] → 'notion'; for
    # 'anthropic.com' → 'anthropic'. Heuristic: take the last non-TLD
    # segment. Good enough for interview scenarios.
    return parts[-2]


_WORD_RE = re.compile(r"[a-z0-9]+")


def _company_tokens(company: str) -> set[str]:
    """Lowercased alphanumeric tokens from a company name. 'Google
    DeepMind' → {'google', 'deepmind'}."""
    return set(_WORD_RE.findall((company or "").lower()))


def match_attendee_to_target(
    attendees_resolved: list[dict], profile: SelfProfile,
) -> dict | None:
    """Find the first attendee whose email domain matches an open
    target_company in the self-profile. Returns the target record (with
    `company`, `tier_company`, etc.) or None. Uses `current_targets` so
    landed / declined companies never match."""
    for target in profile.current_targets:
        tokens = _company_tokens(target.get("company", ""))
        if not tokens:
            continue
        for att in attendees_resolved:
            stem = _domain_stem(_domain_of(att.get("email", "")))
            if stem and stem in tokens:
                return target
    return None


def match_attendee_to_search_stage(
    attendees_resolved: list[dict], profile: SelfProfile,
) -> dict | None:
    """Find the first active_search_pipeline entry whose company matches
    an attendee's email domain. Returns the stage record or None."""
    for stage_rec in profile.active_search_pipeline:
        tokens = _company_tokens(stage_rec.get("company", ""))
        if not tokens:
            continue
        for att in attendees_resolved:
            stem = _domain_stem(_domain_of(att.get("email", "")))
            if stem and stem in tokens:
                return stage_rec
    return None


def _any_attendee_is_recruiter(attendees_resolved: list[dict]) -> bool:
    """True if any resolved attendee has relationship_type=recruiter."""
    for att in attendees_resolved:
        person = att.get("person_data") or {}
        if isinstance(person, dict):
            if (person.get("relationship_type") or "").lower() == "recruiter":
                return True
    return False


def _any_attendee_has_ats_domain(attendees_resolved: list[dict]) -> bool:
    for att in attendees_resolved:
        if is_recruiter_domain(att.get("email", "")):
            return True
    return False


def _organizer_email(event: dict) -> str:
    """Extract the organizer email from an event. gcal-fetch flattens
    to a string; a raw GCal event dict nests under
    {email, displayName, self}. Handle both so the caller doesn't
    have to care which path built the event."""
    org = event.get("organizer")
    if isinstance(org, str):
        return org
    if isinstance(org, dict):
        return str(org.get("email") or "")
    return ""


def _organizer_has_ats_domain(event: dict) -> bool:
    """True when the event organizer sits on a known ATS / recruiting
    domain. Covers the Adobe/Amazon/Google scheduled-interview case
    where no attendees are listed on the invite — the interviewer is
    only named in the description body, so attendee-based signals
    miss. The organizer domain is the strongest remaining tell."""
    return is_recruiter_domain(_organizer_email(event))


def _any_attendee_is_known(attendees_resolved: list[dict]) -> bool:
    for att in attendees_resolved:
        if att.get("person_data"):
            return True
    return False


def _title_has_any_keyword(title: str, keywords: set[str]) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in keywords)


# Description signals that the operator booked a recruiter-initiated meeting
# himself after an external cold-outreach (usually LinkedIn). Matched
# via a regex set rather than substring soup so "recruiter" as part of
# "recruiterly" etc. doesn't false-match. Requires both a
# hiring-process title keyword AND one of these description matches
# before classifying, so ordinary social meetings that happen to
# mention LinkedIn don't fire.
_RECRUITER_DESCRIPTION_PATTERNS = (
    re.compile(r"\brecruiter\b", re.IGNORECASE),
    re.compile(r"\bsourcer\b", re.IGNORECASE),
    re.compile(
        r"\blinkedin\b[\s\S]{0,120}?\b("
        r"follow[- ]?up|following up|connect|chat|opportunity|role|"
        r"conversation|message|reach(?:ed)? out|in touch"
        r")\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(follow[- ]?up|following up|reach(?:ed)? out)\b[\s\S]{0,60}?"
        r"\blinkedin\b",
        re.IGNORECASE,
    ),
)


def _description_has_recruiter_pattern(event: dict) -> bool:
    """True if the event description carries language characteristic
    of a recruiter cold-outreach that ended with the candidate booking
    the meeting themselves. Canonical shape (2026-04-22 Coinbase):
    'Following up on our LinkedIn conversation!'"""
    desc = event.get("description") or ""
    if not desc:
        return False
    for pat in _RECRUITER_DESCRIPTION_PATTERNS:
        if pat.search(desc):
            return True
    return False


def classify_meeting_type(
    event: dict,
    attendees_resolved: list[dict],
    profile: SelfProfile,
) -> str:
    """Return one of 'recruiter-screen', 'hiring-manager',
    'hiring-panel', 'general'. Degrades to 'general' when the
    self-profile carries no data."""
    if not profile.has_profile:
        return "general"

    # 1. Promoted cold recruiter (relationship_type=recruiter on any
    #    attendee's person file) — strongest signal.
    if _any_attendee_is_recruiter(attendees_resolved):
        return "recruiter-screen"

    # 2. ATS / retained-search platform domain on any attendee —
    #    OR on the organizer. Adobe/Amazon-style scheduled interviews
    #    often list only the operator as attendee; the ATS signal lives on
    #    the organizer (schedule@interview.adobe.com).
    if _any_attendee_has_ats_domain(attendees_resolved):
        return "recruiter-screen"
    if _organizer_has_ats_domain(event):
        return "recruiter-screen"

    # 3. Target-company domain match — classify as hiring-manager when
    #    the title names a manager 1:1, else hiring-panel.
    target_match = match_attendee_to_target(attendees_resolved, profile)
    if target_match is not None:
        title = event.get("summary", "")
        if _title_has_any_keyword(title, _HIRING_MANAGER_KEYWORDS):
            return "hiring-manager"
        return "hiring-panel"

    # 4. Hiring-process title keyword + any known attendee (weaker
    #    signal — could be a college friend asking about their own job
    #    search; the known-contact guard avoids firing for random
    #    calendar events with no context).
    title = event.get("summary", "")
    if (_title_has_any_keyword(title, _HIRING_TITLE_KEYWORDS)
            and _any_attendee_is_known(attendees_resolved)):
        return "recruiter-screen"

    # 5. Self-booked cold-recruiter pattern: the operator saw a LinkedIn DM,
    #    booked the meeting himself, so he's the organizer and the
    #    only recruiter signal lives in the description body.
    #    Requires BOTH a hiring-title keyword AND a recruiter-pattern
    #    description match so ordinary social meetings that happen to
    #    mention LinkedIn don't get dragged in.
    if (_title_has_any_keyword(title, _HIRING_TITLE_KEYWORDS)
            and _description_has_recruiter_pattern(event)):
        return "recruiter-screen"

    return "general"


# ---------------------------------------------------------------------------
# LLM prep prompt
# ---------------------------------------------------------------------------


_PREP_INSTRUCTIONS = (
    "You are preparing the operator for a professional meeting (recruiter screen, "
    "hiring manager, or interview). The prep must cover BOTH directions of "
    "the conversation:\n\n"
    "  (A) The recruiter / interviewer has things they want to learn about "
    "the operator — his experience, what makes him interested in the role, whether "
    "he's a real fit. The prep MUST give the operator a clear pitch narrative plus "
    "a specific 'why this role, why now' angle so the call leaves the "
    "recruiter able to advocate for him with the hiring manager.\n\n"
    "  (B) the operator has things he wants to learn to decide if the role is a "
    "match. The prep MUST give him a small number of sharp evaluation "
    "questions, anchored in his level & scope bar.\n\n"
    "Reason through the call in THIS order before writing the output:\n"
    "  1. RECIPIENT MODEL — what does the recruiter / interviewer need to "
    "leave this call with? (They're evaluating fit + deciding whether to "
    "advance the operator.)\n"
    "  2. OBJECTIVE — the operator's dual goal: (i) evaluate whether this is a "
    "real match against his level & scope bar, and (ii) make a clean case "
    "for fit so the recruiter can confidently advance him if (i) clears.\n"
    "  3. STRATEGY — translate both into prep output: a pitch narrative + "
    "compelling-angle for (B)-meets-(i), fit evidence for (A), and "
    "evaluation questions for (ii).\n\n"
    "Constraints:\n"
    "- Speak in terms of the operator's scope, outcomes, and leadership traits — "
    "not verbatim quotes from employer-proprietary strategy documents.\n"
    "- fit_pitch should be a short NARRATIVE (not bullets) — the story "
    "the operator tells when asked 'walk me through your background' — anchored "
    "in the specific arc (or two arcs) that map to THIS role. Reference "
    "concrete outcomes, not resume adjectives.\n"
    "- compelling_angle must answer 'why this role / why are you listening "
    "now?' honestly. If the operator is in late-stage with other companies, say "
    "what specifically about this role / company would pull him anyway. "
    "Avoid boilerplate enthusiasm.\n"
    "- fit_evidence should be 3 concrete proof-point bullets — specific "
    "things the operator has done that map to this role's demands. These are "
    "drop-in evidence, not the pitch narrative.\n"
    "- evaluation_questions should surface information the recruiter can "
    "answer in the meeting, not things the operator would only learn from the "
    "company's public materials. Anchor at least one on the level & scope "
    "bar.\n"
    "- When a COMPANY BRIEF block is provided, GROUND fit_pitch, "
    "compelling_angle, and evaluation_questions in its specifics. Do not "
    "produce generic content (e.g. 'tell me about the team structure') "
    "when the brief names concrete products, news, or role details you "
    "could anchor a question to instead. The research-suggested questions "
    "are a starting point — refine or replace, but do not fabricate "
    "generic alternatives when concrete signal exists.\n"
    "- red_flags are optional. If the inbound context is clean, return []. "
    "Only flag when you see a genuine level/scope mismatch, compensation "
    "signal to press on, or a time-pressure mismatch with active pipeline.\n"
    "- Output ONLY valid JSON matching the schema. No preamble, no "
    "trailing prose."
)


_PREP_SCHEMA_HINT = (
    'Return ONLY a JSON object matching this schema (no preamble, no '
    'trailing prose):\n'
    '{\n'
    '  "recipient_model": "1-2 sentences on what the recruiter needs '
    'from this call",\n'
    '  "objective": "1 sentence on the operator\'s dual goal (evaluate + pitch)",\n'
    '  "fit_pitch": "3-5 sentence narrative — the story the operator tells '
    'mapping his specific track record to THIS role",\n'
    '  "compelling_angle": "1-2 sentences answering \'why this role, '
    'why listen now\' honestly",\n'
    '  "fit_evidence": ["concrete proof-point 1", "proof-point 2", '
    '"proof-point 3"],\n'
    '  "evaluation_questions": ["Q1", "Q2", "Q3"],\n'
    '  "red_flags": ["R1", ...],\n'
    '  "prep_summary": "one-line framing (15-25 words)"\n'
    '}'
)


def _format_attendee_block(attendees_resolved: list[dict]) -> str:
    lines = []
    for att in attendees_resolved:
        name = att.get("name") or "(no name)"
        email = att.get("email") or ""
        person = att.get("person_data") or {}
        if isinstance(person, dict) and person.get("relationship_type"):
            rel = f" · {person['relationship_type']}"
        else:
            rel = ""
        lines.append(f"- {name} ({email}){rel}")
    return "\n".join(lines) if lines else "(no attendees resolved)"


def _format_self_block(profile: SelfProfile) -> str:
    parts = []
    if profile.level_bar_text:
        parts.append(f"Level & scope bar:\n{profile.level_bar_text}")
    if profile.strength_themes:
        top = profile.strength_themes[:5]
        theme_lines = []
        for t in top:
            theme = t.get("theme") or t.get("content") or ""
            if theme:
                theme_lines.append(f"- {theme}")
        if theme_lines:
            parts.append("Strength themes:\n" + "\n".join(theme_lines))
    return "\n\n".join(parts) if parts else "(no self-context loaded)"


def _format_pipeline_block(stage_match: dict | None,
                           profile: SelfProfile) -> str:
    if stage_match is not None:
        co = stage_match.get("company", "?")
        st = stage_match.get("stage", "?")
        when = stage_match.get("last_signal_date", "?")
        notes = (stage_match.get("notes") or "").strip()
        return f"Active pipeline with {co} — stage {st} (last signal {when}).\n{notes}"
    late = profile.late_stage_searches
    if late:
        names = ", ".join(s.get("company", "?") for s in late[:5])
        return f"Not in active pipeline with this company. Late-stage with others: {names}."
    return "Not in active pipeline with this company."


def resolve_company_name_for_prep(
    *,
    event: dict,
    attendees_for_classifier: list[dict],
    target_match: dict | None,
) -> str | None:
    """Pick the company name to feed into company_research.research_company.

    Resolution order (most reliable first):
      1. target_match["company"] — the operator's explicit shortlist hit.
      2. _extract_company_token(event) from the title — handles self-
         booked LinkedIn cold-recruiter invites like "Interview with
         Coinbase" cleanly.
      3. Attendee-domain inference via _infer_company_from_email,
         skipping attendees whose email_source is linkedin_relay
         (their @linkedin.com address is a routing artifact, not the
         real company).

    Returns None when no reliable signal is available — the caller
    then skips enrichment rather than researching a wrong company.

    Regression 2026-04-23: Coinbase prep researched LinkedIn because
    the only attendee resolved to hit-reply@linkedin.com via Gmail
    lookup, and the old path naively inferred "Linkedin" from the
    domain stem.
    """
    # Local import so this module stays importable without the shared
    # libs on path during unit tests that only exercise pure helpers.
    try:
        from agents.shared.gmail_recruiter_lookup import _extract_company_token
        from agents.shared.recruiter_extract_lib import _infer_company_from_email
    except ImportError:
        from gmail_recruiter_lookup import _extract_company_token  # type: ignore
        from recruiter_extract_lib import _infer_company_from_email  # type: ignore

    if target_match and target_match.get("company"):
        return target_match["company"]

    token = _extract_company_token(event)
    if token:
        return token

    for att in attendees_for_classifier:
        if att.get("email_source") == "linkedin_relay":
            continue
        inferred = _infer_company_from_email(att.get("email", ""))
        if inferred:
            return inferred

    return None


def _format_company_brief_block(company_brief: dict | None) -> str:
    """Render the COMPANY BRIEF block from company_research.CompanyBrief's
    to_prompt_dict(). Empty dict (errored brief) → empty string so the
    caller's f-string splice cleanly omits the section."""
    if not company_brief:
        return ""
    company_name = company_brief.get("company_name", "")
    what_they_do = company_brief.get("what_they_do", "")
    stage = company_brief.get("stage_signal", "unknown")
    confidence = company_brief.get("confidence", "low")
    recent_news = company_brief.get("recent_news") or []
    role_ctx = company_brief.get("role_context") or {}
    operator_fit = company_brief.get("operator_fit") or {}

    lines: list[str] = []
    lines.append(
        f"COMPANY BRIEF (external research on {company_name} — ground pitch + "
        f"questions in these specifics; do not produce generic content when "
        f"concrete signal is available)"
    )
    if what_they_do:
        lines.append(f"What they do: {what_they_do}")
    lines.append(f"Stage: {stage} (research confidence: {confidence})")

    if recent_news:
        lines.append("Recent news:")
        for item in recent_news[:5]:
            bullet = (item.get("bullet") or "").strip() if isinstance(item, dict) else str(item)
            dated = (item.get("dated") or "").strip() if isinstance(item, dict) else ""
            lines.append(f"  - [{dated}] {bullet}" if dated else f"  - {bullet}")

    if role_ctx:
        lines.append("Role context (from JD signal):")
        if role_ctx.get("title"):
            lines.append(f"  Title: {role_ctx['title']}")
        if role_ctx.get("team_hint"):
            lines.append(f"  Team: {role_ctx['team_hint']}")
        if role_ctx.get("level_band_hint"):
            lines.append(f"  Level band: {role_ctx['level_band_hint']}")
        if role_ctx.get("scope_hint"):
            lines.append(f"  Scope: {role_ctx['scope_hint']}")

    angles = operator_fit.get("strength_angles") or []
    concerns = operator_fit.get("concerns") or []
    hooks = operator_fit.get("hooks_to_drop") or []
    qs = operator_fit.get("questions_to_ask") or []
    if angles:
        lines.append("Strength angles (use in fit_pitch):")
        for a in angles[:5]:
            lines.append(f"  - {a}")
    if concerns:
        lines.append("Concerns to probe (use in evaluation_questions / red_flags):")
        for c in concerns[:5]:
            lines.append(f"  - {c}")
    if hooks:
        lines.append("Hooks (reference these to prove the operator read the room):")
        for h in hooks[:4]:
            lines.append(f"  - {h}")
    if qs:
        lines.append(
            "Research-suggested questions (starting point — refine when you "
            "can do better; prefer these over generic 'tell me about the "
            "team' fabrications):"
        )
        for q in qs[:5]:
            lines.append(f"  - {q}")

    return "\n".join(lines)


def build_llm_prep_prompt(
    *,
    event: dict,
    attendees_resolved: list[dict],
    self_profile: SelfProfile,
    meeting_type: str,
    target_company_match: dict | None,
    search_stage_match: dict | None,
    company_brief: dict | None = None,
) -> str:
    """Assemble the user-side prompt for the LLM. Pure — testable without
    a network call."""
    title = event.get("summary", "(no title)")
    description = (event.get("description") or "").strip()

    target_block = ""
    if target_company_match is not None:
        tier = target_company_match.get("tier_company") or "?"
        notes = (target_company_match.get("notes") or "").strip()
        target_block = (
            f"TARGET COMPANY MATCH\n"
            f"Company: {target_company_match.get('company', '?')} "
            f"(tier {tier})\n"
            f"{notes}\n"
        )

    pipeline_block = _format_pipeline_block(search_stage_match, self_profile)

    brief_block = _format_company_brief_block(company_brief)
    brief_section = f"{brief_block}\n\n" if brief_block else ""

    return (
        f"MEETING CONTEXT\n"
        f"Title: {title}\n"
        f"Meeting type: {meeting_type}\n"
        f"Description:\n{description[:500] if description else '(none)'}\n\n"
        f"ATTENDEES\n"
        f"{_format_attendee_block(attendees_resolved)}\n\n"
        f"SELF CONTEXT\n"
        f"{_format_self_block(self_profile)}\n\n"
        f"{brief_section}"
        f"{target_block}"
        f"PIPELINE\n"
        f"{pipeline_block}\n\n"
        f"{_PREP_SCHEMA_HINT}\n"
    )


def build_llm_prep(
    *,
    event: dict,
    attendees_resolved: list[dict],
    self_profile: SelfProfile,
    meeting_type: str,
    company_brief: dict | None = None,
) -> dict[str, Any]:
    """Call the LLM and return a structured prep dict. Never raises —
    returns `{"error": <reason>}` on failure so the rendering layer can
    fall through to context-only."""
    target_match = match_attendee_to_target(attendees_resolved, self_profile)
    stage_match = match_attendee_to_search_stage(attendees_resolved, self_profile)

    prompt = build_llm_prep_prompt(
        event=event,
        attendees_resolved=attendees_resolved,
        self_profile=self_profile,
        meeting_type=meeting_type,
        target_company_match=target_match,
        search_stage_match=stage_match,
        company_brief=company_brief,
    )

    try:
        result = infer(
            prompt=prompt,
            instructions=_PREP_INSTRUCTIONS,
            json_mode=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"infer raised: {exc}"}

    if not result.ok:
        return {"error": getattr(result, "error", "") or "llm call not ok"}

    try:
        parsed = json.loads(result.text)
    except (json.JSONDecodeError, TypeError) as exc:
        return {"error": f"malformed JSON: {exc}"}

    if not isinstance(parsed, dict):
        return {"error": "LLM returned non-object"}

    # Normalize to the expected shape — missing keys default to safe values.
    # Accept legacy field names (questions_to_ask / talking_points) for
    # backward compatibility with prior prompt schemas.
    return {
        "recipient_model": str(parsed.get("recipient_model") or "").strip(),
        "objective": str(parsed.get("objective") or "").strip(),
        "fit_pitch": str(parsed.get("fit_pitch") or "").strip(),
        "compelling_angle": str(parsed.get("compelling_angle") or "").strip(),
        "fit_evidence": list(
            parsed.get("fit_evidence") or parsed.get("talking_points") or []
        )[:5],
        "evaluation_questions": list(
            parsed.get("evaluation_questions")
            or parsed.get("questions_to_ask") or []
        )[:5],
        "red_flags": list(parsed.get("red_flags") or [])[:5],
        "prep_summary": str(parsed.get("prep_summary") or "").strip(),
        "target_company_match": target_match,
        "search_stage_match": stage_match,
    }
