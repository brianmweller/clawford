"""role_archive_lib — pure helpers for the per-role archive synthesizer.

Groups Stage-1 archive-index records by synthesis role, weights them by
(class × signal_score), and builds per-role synthesis prompts.

The synthesizer script (role-archive-synthesize.py) owns LLM I/O and
filesystem writes; this module is import-only and testable.

Routing semantics:
  - Filesystem records with role ∈ {twitch, amazon, linkedin, airbnb,
    netflix} → that role directly.
  - Workflowy records (role=meetings) → role looked up from their
    chronological_date against the role-timeline ranges.
  - Bios (role=bio) route by chronological_date if known; undated bios
    fall to a cross-role 'bio' bucket.
  - Job-search (role=job-search) stays in its own 'job-search' bucket.
  - Unknown / missing date → 'unknown' bucket (synthesizer typically
    drops these).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from career_archive_lib import extract_text  # noqa: E402
from role_timeline_lib import role_for_date  # noqa: E402
from workflowy_walker import iter_meeting_records  # noqa: E402


# Relative weight per class for the profile-synthesis task. Drives
# ranking within each role so the top-N records fed to the LLM are the
# highest-signal ones. Tune conservatively — profile synthesis weights
# bios, accomplishments, tenets, and feedback most.
CLASS_WEIGHTS: dict[str, float] = {
    "bio": 1.0,
    "feedback_received": 1.0,
    "accomplishment": 0.95,
    "okr_kpi": 0.90,
    "tenet_or_framework": 0.90,
    "strategic_doc": 0.85,
    "roadmap": 0.75,
    "feedback_given": 0.70,
    "job_search_material": 0.50,
    "email_or_correspondence": 0.40,
    "contact_list": 0.30,
    "ephemeral": 0.10,
    "skip": 0.0,
}


# Filesystem roles that map 1:1 to synthesis roles. Everything else
# (meetings, bio, job-search) is routed through specific logic.
DIRECT_ROLES = {"twitch", "amazon", "linkedin", "airbnb", "netflix"}


# Default cap on records sent to the LLM per role. 50 × ~150 tokens per
# summary = ~7500 tokens, well under budget.
DEFAULT_LIMIT = 50


# How many top-ranked records get RAW content (extracted from the
# source document) vs just a summary. Raw content costs ~2500 chars
# per record but closes the "summaries-of-summaries" fidelity gap for
# the load-bearing evidence.
DEFAULT_RAW_LIMIT = 15


# Max characters of raw content per record when feeding the synthesis
# prompt. Beyond this, truncate. 3000 chars ≈ 750 tokens.
DEFAULT_RAW_CHARS = 3000


# Classes whose raw content most matters for synthesis. Everything else
# uses just the classifier summary. feedback_received matters because it
# IS the feedback-about-the operator evidence; bio captures self-framing;
# strategic_doc / tenet_or_framework / accomplishment / okr_kpi are
# the operator's actual authored work.
RAW_CONTENT_CLASSES = {
    "feedback_received",
    "bio",
    "accomplishment",
    "tenet_or_framework",
    "strategic_doc",
    "okr_kpi",
}


def effective_score(record: dict) -> float:
    """record → float in [0, 1]. Combines signal_score with class weight.
    Used for ranking records within a role."""
    cls = record.get("class", "skip")
    weight = CLASS_WEIGHTS.get(cls, 0.0)
    signal = float(record.get("signal_score", 0.0))
    return weight * signal


def rank_records(records: list[dict], *, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Sort records by effective_score descending and truncate to limit."""
    ordered = sorted(records, key=effective_score, reverse=True)
    return ordered[:limit]


def group_records_by_role(
    index: dict,
    timeline_ranges: list[dict],
    *,
    search_timeline_ranges: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Group archive-index file entries by the role they belong to for
    profile synthesis.

    Routing:
      - role ∈ DIRECT_ROLES → that role
      - role='meetings' → role_for_date(chronological_date, ranges)
      - role='bio' → role_for_date if date known, else 'bio'
      - role='job-search':
          * If search_timeline_ranges provided → route by chronological_date
            to the matching search-round bucket. Pre-2015 dates and
            dateless records go to 'search-undated'.
          * Otherwise → flat 'job-search' bucket (legacy behavior).
      - otherwise → 'unknown'

    Returns dict[role → list[entry]]. Empty roles are omitted.
    """
    files = (index or {}).get("files") or {}
    groups: dict[str, list[dict]] = {}

    for path, entry in files.items():
        rec = dict(entry)
        rec.setdefault("path", path)
        src_role = rec.get("role", "")

        if src_role in DIRECT_ROLES:
            target = src_role
        elif src_role == "meetings":
            target = role_for_date(rec.get("chronological_date", ""), timeline_ranges)
        elif src_role == "bio":
            if rec.get("chronological_date"):
                target = role_for_date(rec["chronological_date"], timeline_ranges)
                # If the date doesn't map to a known role (e.g., pre-twitch),
                # fall through to the cross-role bio bucket
                if target == "unknown":
                    target = "bio"
            else:
                target = "bio"
        elif src_role == "job-search":
            if search_timeline_ranges:
                target = _route_job_search_to_search_round(rec, search_timeline_ranges)
            else:
                target = "job-search"
        else:
            target = "unknown"

        groups.setdefault(target, []).append(rec)

    return groups


# Classifier hallucinates historical years from content occasionally
# (e.g., "Great Depression 1929" → chronological_date=1929). Any
# pre-2015 date on a job-search record is almost certainly a misfire;
# route to search-undated rather than to a search round.
_MIN_VALID_SEARCH_YEAR = 2015


def _route_job_search_to_search_round(rec: dict, search_ranges: list[dict]) -> str:
    """Route a single job-search record to its search-round bucket."""
    date = rec.get("chronological_date", "") or ""
    # Guard against classifier-artifact dates (1880-2014)
    if date and len(date) >= 4:
        try:
            year = int(date[:4])
            if year < _MIN_VALID_SEARCH_YEAR:
                return "search-undated"
        except ValueError:
            pass
    if not date:
        return "search-undated"
    target = role_for_date(date, search_ranges)
    if target == "unknown":
        return "search-undated"
    return target


def build_workflowy_text_map(nodes: list[dict], *, max_chars: int = 5000) -> dict[str, str]:
    """From a fresh /nodes-export, build {wf://id → flattened text} for
    every meeting node. Used by the synthesizer to fetch raw content for
    Workflowy-sourced records; archive-index.json doesn't store text, so
    we re-derive it from a live export.
    """
    out: dict[str, str] = {}
    for rec in iter_meeting_records(nodes):
        text = rec.get("text_excerpt") or ""
        out[rec["path"]] = text[:max_chars] if max_chars else text
    return out


def fetch_raw_content(
    record: dict,
    *,
    workflowy_texts: dict[str, str] | None = None,
    max_chars: int = DEFAULT_RAW_CHARS,
) -> str:
    """Return raw extracted text for a record, or empty string if
    unavailable.

    - Filesystem records (path on disk): re-run extract_text().
    - Workflowy records (path starts with wf://): look up in the
      workflowy_texts map (built from a fresh /nodes-export).

    Truncated to max_chars. Never raises.
    """
    path_str = str(record.get("path", ""))
    if not path_str:
        return ""
    if path_str.startswith("wf://"):
        if workflowy_texts is None:
            return ""
        text = workflowy_texts.get(path_str, "") or ""
        return text[:max_chars] if max_chars else text
    try:
        p = Path(path_str)
        text, _ = extract_text(p)
    except Exception:  # noqa: BLE001 — extractors can raise a zoo of formats
        return ""
    if not text:
        return ""
    return text[:max_chars] if max_chars else text


def pick_raw_records(
    ranked_records: list[dict],
    *,
    raw_limit: int = DEFAULT_RAW_LIMIT,
    raw_classes: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split ranked records into (get-raw-content, summary-only) based
    on class + ranking.

    Priority order for the raw tier:
      1. Records with priority_override=True (hand-flagged by the operator as
         load-bearing regardless of class — e.g., a comprehensive
         meeting-notes doc the classifier underestimated). These go to
         raw first, up to raw_limit.
      2. Records whose class is in raw_classes, in ranking order,
         filling remaining slots.

    Everything else becomes summary-only context.
    """
    classes = raw_classes or RAW_CONTENT_CLASSES

    # Pass 1: hand-flagged overrides win first
    raw: list[dict] = []
    rest: list[dict] = []
    for rec in ranked_records:
        if rec.get("priority_override") and len(raw) < raw_limit:
            raw.append(rec)
        else:
            rest.append(rec)

    # Pass 2: class-based raw candidates fill remaining slots
    summary_only: list[dict] = []
    for rec in rest:
        if len(raw) < raw_limit and rec.get("class") in classes:
            raw.append(rec)
        else:
            summary_only.append(rec)
    return raw, summary_only


def build_meeting_patterns_prompt(role_label: str, meeting_records: list[dict], *, max_records: int = 250) -> str:
    """Build a prompt to extract meeting-footprint patterns from a role's
    Workflowy records. Uses the classifier SUMMARIES (not raw content) —
    Stage 1 already normalized meeting titles + counterparties into the
    summary field.

    The LLM returns structured JSON with recurring participants,
    cross-functional group patterns, and a short narrative paragraph
    about the operator's meeting footprint during this period.
    """
    # Prefer meeting records (role=meetings), but also tolerate
    # filesystem 1:1 notes classified as email_or_correspondence etc.
    lines: list[str] = []
    for rec in meeting_records[:max_records]:
        date = rec.get("chronological_date") or "?"
        cls = rec.get("class", "?")
        summary = (rec.get("summary") or "").replace("\n", " ").strip()
        if summary:
            lines.append(f"{date} [{cls}]: {summary[:200]}")
    data_block = "\n".join(lines) if lines else "(no meeting records)"

    return f"""Below is a list of Sam Smith's meeting-record summaries from his
{role_label} period. Each line is one meeting: date, class tag, and a
short descriptive summary mentioning who was in the meeting and what
it was about.

{data_block}

Extract the operator's meeting footprint during this period. Return valid JSON
with exactly this shape:

{{
  "recurring_participants": [
    {{"name": "<person name>", "approx_count": <int>, "cadence_guess": "weekly|biweekly|monthly|ad-hoc", "relationship_guess": "manager|skip-level|direct-report|peer|partner|external|unknown"}}
  ],
  "cross_functional_groups": [
    {{"group": "<group name like Engineering, Product, Finance, Research>", "cadence": "weekly|biweekly|monthly|ad-hoc", "likely_role": "lead|attendee|cross-functional-partner"}}
  ],
  "notable_meeting_types": [
    "<e.g., marketplace DS standup, leadership sync, offsite, interview loop, skip-level>"
  ],
  "summary_paragraph": "<80-120 words describing who the operator met with, on what cadence, and what the meeting footprint suggests about his scope/influence in this role. Avoid naming specific individuals not essential to the narrative.>"
}}

Rules:
- Only include a recurring_participant if the person's name appears
  >=3 times across distinct meeting records. Don't invent counts.
- Don't use identifiable quotes or judgments about specific people.
- If a section has no signal, return an empty array or short note.
- Output ONLY JSON, no surrounding prose or code fences.
"""


def parse_meeting_patterns_response(raw: str) -> dict:
    """Parse the LLM's meeting-patterns JSON response. Returns a dict
    with the four expected keys (empty/default values on parse failure
    or missing keys). Never raises."""
    import json as _json
    import re as _re
    if not raw:
        return _empty_patterns()
    # Strip ```json fences if the LLM emitted them despite being told not to
    stripped = raw.strip()
    fence_match = _re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", stripped, flags=_re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1)
    try:
        obj = _json.loads(stripped)
    except _json.JSONDecodeError:
        return _empty_patterns()
    if not isinstance(obj, dict):
        return _empty_patterns()
    return {
        "recurring_participants": obj.get("recurring_participants") or [],
        "cross_functional_groups": obj.get("cross_functional_groups") or [],
        "notable_meeting_types": obj.get("notable_meeting_types") or [],
        "summary_paragraph": str(obj.get("summary_paragraph") or ""),
    }


def _empty_patterns() -> dict:
    return {
        "recurring_participants": [],
        "cross_functional_groups": [],
        "notable_meeting_types": [],
        "summary_paragraph": "",
    }


def build_synthesis_prompt(
    range_info: dict,
    raw_content_records: list[tuple[dict, str]] | None = None,
    summary_records: list[dict] | None = None,
    meeting_patterns: dict | None = None,
    *,
    records: list[dict] | None = None,
) -> str:
    """Build the LLM prompt for one role's archive synthesis (v2).

    Accepts three tiers of evidence:
      - raw_content_records: [(record, raw_text)] — highest fidelity,
        for the top feedback_received / bio / accomplishment / strategic
        / tenet / okr records. the operator's actual words, artifacts, feedback.
      - summary_records: records using the 30-word classifier summary —
        for breadth context. The prompt tells the LLM these are lower-
        fidelity summaries, not to quote from them directly.
      - meeting_patterns: optional dict from meeting_patterns_lib with
        keys like recurring_participants, cross_functional_groups, and
        a summary_paragraph. Used to ground the "Meeting patterns"
        section and the cross-functional reach signal.

    Backward-compat: if `records=` is passed (old one-list signature),
    it's treated as summary_records with no raw content. Used by the
    existing tests until they're updated.
    """
    # Back-compat shim for old callers
    if records is not None and raw_content_records is None and summary_records is None:
        summary_records = records

    raw_content_records = raw_content_records or []
    summary_records = summary_records or []

    role = range_info["role"]
    start = range_info["start"] or "before time"
    end = range_info["end"]
    label = range_info["label"]

    # Block 1: raw-content evidence blocks
    raw_blocks: list[str] = []
    for rec, raw_text in raw_content_records:
        cls = rec.get("class", "?")
        date = rec.get("chronological_date") or "?"
        path = str(rec.get("path", ""))
        # Show short path — last 2 components or the wf:// tail
        if path.startswith("wf://"):
            short_path = path
        else:
            parts = path.replace("\\", "/").split("/")
            short_path = "/".join(parts[-2:])
        raw_blocks.append(
            f"[class={cls} | date={date} | path={short_path}]\n"
            f"{raw_text.strip()}\n"
        )
    raw_section = ("\n---\n".join(raw_blocks)) if raw_blocks else "(none)"

    # Block 2: summary-only records
    summary_lines: list[str] = []
    for rec in summary_records:
        cls = rec.get("class", "?")
        score = rec.get("signal_score", 0.0)
        date = rec.get("chronological_date") or "?"
        summary = (rec.get("summary") or "").replace("\n", " ").strip()
        summary_lines.append(f"[{cls} | score={score:.2f} | date={date}] {summary}")
    summary_block = "\n".join(summary_lines) if summary_lines else "(none)"

    # Block 3: meeting patterns
    patterns_block = "(no meeting-pattern data available)"
    if meeting_patterns:
        import json as _json
        try:
            patterns_block = _json.dumps(meeting_patterns, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            patterns_block = str(meeting_patterns)

    raw_count = len(raw_content_records)
    summary_count = len(summary_records)

    return f"""You are synthesizing a per-role career summary for Sam Smith's
{role} tenure ({start} to {end}, {label}).

You have THREE layers of evidence. Use them in priority order:

(1) RAW CONTENT — the actual text of {raw_count} highest-signal documents
    from this period. the operator's own words, actual formal feedback, actual
    authored artifacts. Ground every specific claim in these.
(2) SUMMARY CONTEXT — one-line summaries of {summary_count} additional
    supporting records. Use for breadth and pattern-confirmation, but
    don't quote from or over-weight them.
(3) MEETING PATTERNS — structured extract of the operator's meeting footprint
    during this period. Grounds the cross-functional-reach signal.

CONFIDENTIALITY RULES:
  - Speak in terms of the operator's scope, decisions, outcomes, influence.
  - Do NOT quote proprietary product strategy verbatim.
  - Do NOT use identifiable judgments about specific others — aggregate
    themes ("growth area: pacing") not named attributions ("X said Y
    about the operator's pacing").
  - Don't fabricate numbers. If a record mentions a specific metric,
    restate it; if not, use qualitative language.

OUTPUT: markdown with exactly these h2 sections, in this order. If a
section genuinely has no supporting evidence, write "(no records for
this section)." — do NOT invent.

## Scope & team
One short paragraph grounded in raw-content evidence. Team size,
reporting line, span-of-control where visible.

## OKRs and metrics owned
Bullet list. Each bullet = one specific OKR / KPI the operator owned or
authored, with year/quarter when available. Cite the titled artifact
(e.g., "Q2 2020 Central Science OKRs") where possible.

## Tenets, frameworks, and operating models authored
Bullet list of named frameworks the operator wrote. Quote or paraphrase the
actual tenet names from raw content when available.

## Accomplishments
Bullet list of launches, orgs built, metrics moved, hires landed. Be
specific and grounded — prefer artifact titles from raw content.

## Meeting patterns
Short paragraph + bullet list grounded in the MEETING PATTERNS section
below. Who the operator met with regularly (top recurring participants),
cross-functional groups, and what his meeting footprint suggests about
scope/influence in this role. If meeting-pattern data is absent,
write "(no meeting-pattern data for this section)."

## Demonstrated strengths & development areas

### Strengths
Triangulate across THREE signal types and label each strength with
which evidence supports it:
  - "Evidence from authored work:" — pattern inferred from the
    volume / scope / topics of the operator's strategic_doc / tenet /
    accomplishment / okr records.
  - "Evidence from formal feedback:" — pattern from raw-content
    feedback_received records (only claim if actually supported).
  - "Evidence from self-framing:" — pattern from bio / CV records.

Aim for 3-6 strengths; each clearly grounded. No claim without a
cited evidence type.

### Development areas
STRICT RULES:
  - A claim here requires evidence from TWO OR MORE records from
    DISTINCT SOURCES. "Distinct source" means a different person or a
    genuinely different evaluation context. Two records involving the
    same person do NOT count as two sources — e.g.:
      * A 1:1 note with Person X + an offboarding recap from Person X
        = ONE source (same person, continuing thread).
      * A manager 1:1 note + a self-authored retro about the same
        episode = ONE source (same perspective).
      * Formal year-end feedback from Manager A + informal mid-year
        note from Manager A = ONE source.
      * Formal year-end from Manager A + a 360 note from peer B + a
        coaching doc from external coach C = THREE distinct sources.
    If records that appear to say the same thing actually trace to
    one person's perspective, the theme is NOT supported and should
    be omitted from this section.
  - If a single source flags something, mention it at most in-line as
    context elsewhere (e.g., under a strength as a nuance) — never as
    a general development-area bullet.
  - If the balance of evidence (positive formal feedback, pattern of
    successful authored work, executive-level framework authorship)
    contradicts a potential development area, OMIT it.
  - It's valid and HONEST to write "(the balance of evidence in this
    role does not surface consistent development areas.)" or to list
    only 1-2 items. DO NOT invent development areas to fill space.

## Departure context
If raw-content records show exit rationale (layoff, resignation,
transition), summarize. Otherwise write "(no records for this
section)."

## Evidence sources used
- Raw content: {raw_count} records fed as full text
- Summary context: {summary_count} additional records
- Meeting patterns: {'present' if meeting_patterns else 'absent'}

===============================================
(1) RAW CONTENT
===============================================

{raw_section}

===============================================
(2) SUMMARY CONTEXT
===============================================

{summary_block}

===============================================
(3) MEETING PATTERNS
===============================================

{patterns_block}
"""
