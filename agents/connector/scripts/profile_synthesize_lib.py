"""profile_synthesize_lib — narrative profile.md synthesis (Stage 1.2).

Reads the outputs of Stages 3 (archives) and 4 (structured facts) plus
priority-flagged raw docs, and asks the LLM to produce a consolidated
markdown narrative. This is the artifact Huckle reads when drafting
replies to cold recruiter inbounds, and Murphy reads when prepping
recruiter / hiring-manager conversations.

Pure helpers — the runner (profile-synthesize.py) owns LLM I/O and
filesystem writes.
"""
from __future__ import annotations

import json


# Sections the prompt requests. Order matters (narrative arc).
_SECTIONS = [
    "Self-description",
    "Track record",
    "Excited by",
    "Great at",
    "Anti-patterns",
    "Leadership / operating style",
    "Target-role shape",
]


def format_facts_block(facts: dict[str, list[dict]]) -> str:
    """Render structured facts as JSON sections, labeled by type.

    Keeps the per-type structure visible so the LLM can reference
    specific fields (e.g., 'as target_company.Anthropic shows').
    """
    if not facts:
        return "(no structured facts available)"
    parts: list[str] = []
    for fact_type, records in facts.items():
        if not records:
            continue
        parts.append(f"### {fact_type} ({len(records)} records)")
        parts.append(json.dumps(records, indent=2, ensure_ascii=False))
        parts.append("")
    return "\n".join(parts) if parts else "(no structured facts available)"


def _format_archives_block(archives: dict[str, str]) -> str:
    """Each archive file is labeled + rendered verbatim."""
    if not archives:
        return "(no archives available)"
    parts: list[str] = []
    for name in sorted(archives):
        parts.append(f"======= {name} =======")
        parts.append(archives[name].strip())
        parts.append("")
    return "\n".join(parts)


def _format_priority_raw_block(priority_docs: list[tuple[str, str]]) -> str:
    """Priority-flagged raw docs get labeled sections so the LLM can
    attribute specific phrasing to them (e.g., 'as the operator's Consolidated
    Prep framing shows')."""
    if not priority_docs:
        return "(no priority-flagged raw documents provided)"
    parts: list[str] = []
    for path, content in priority_docs:
        # Show last 2 path components only — no full Windows paths
        parts.append(f"======= {path} =======")
        parts.append(content.strip())
        parts.append("")
    return "\n".join(parts)


def build_profile_prompt(
    facts: dict[str, list[dict]],
    archives: dict[str, str],
    priority_raw_docs: list[tuple[str, str]],
) -> str:
    """Compose the single LLM prompt that produces self/profile.md.

    facts: {fact_type → list[record]} from self/facts/*.json
    archives: {filename → markdown content} from self/archives/*.md
    priority_raw_docs: [(path, raw_text), ...] from priority_override=True
      records in the archive-index
    """
    facts_block = format_facts_block(facts)
    archives_block = _format_archives_block(archives)
    priority_block = _format_priority_raw_block(priority_raw_docs)

    sections_md = "\n".join(f"## {s}" for s in _SECTIONS)

    return f"""You are writing Sam Smith's consolidated professional profile —
the canonical narrative used by downstream agents to represent him to
external audiences.

DOWNSTREAM CONSUMERS:
  - Huckle Cat (email agent): reads this when drafting replies to cold
    recruiter inbounds; uses it to calibrate voice, evaluate role fit
    against target_company tier, and position the operator's strengths.
  - Sergeant Murphy (meetings agent): reads this when prepping the operator
    for recruiter / hiring-manager / exec conversations; uses it to
    generate role-appropriate talking points and clarifying questions.

Both consumers will also query the structured JSON facts below
programmatically — profile.md is the narrative layer on top, NOT a
replacement for the facts.

INPUTS (three tiers, weighted in priority order):

(1) STRUCTURED FACTS — authoritative, emit claims grounded in these:
{facts_block}

(2) EVIDENCE-CITED ARCHIVES — per-role and per-search-round synthesis:
{archives_block}

(3) PRIORITY-FLAGGED RAW DOCS — the operator's own unfiltered words, HIGHEST
fidelity for voice and framing:
{priority_block}

OUTPUT: markdown with exactly these h2 sections, in this order:

{sections_md}

SECTION GUIDANCE:

## Self-description
One paragraph (~80-120 words). The 1st-person pitch the operator would use at
the start of an exec introduction. Should feel like the operator's actual
voice — grounded in his bios, CVs, and Consolidated Prep framing. Not
a generic "I am a data science leader" opener — include the specific
through-line (marketplace science, pricing, personalization, causal
ML) and the through-line across roles.

## Track record
5-10 bullets. Each = one headline outcome with role + metric where
available. Draw from major_accomplishment and tenet_authored facts
directly — use the specific artifacts named there, not paraphrases.
Organized either chronologically or by impact type — pick whichever
tells the tightest story.

## Excited by
Bullet list of problem domains / role shapes / company types the operator is
currently energized by. Weight heavily toward search-post-airbnb
evidence (his CURRENT framing, not historical). Include:
  - The intersection of {{field1}} and {{field2}} that shows up repeatedly
  - Marketplace science, personalization, causal ML, pricing themes
  - Company types: frontier AI labs + senior roles at strong brands
  - Role shapes: exec (CTO/CDO/Head of X) scope, org-building mandate

## Great at
4-6 bullets. Strength themes from strength_theme facts, each with:
  - The specific capability
  - What evidence type supports it (authored work / formal feedback /
    self-framing)
  - One or two concrete examples (named artifacts / metrics)

## Anti-patterns
2-4 bullets. What the operator has turned down or moved away from — inferred
from:
  - Departure contexts across roles
  - Job-search decision frameworks
  - Consolidated Prep framing around what he ISN'T looking for
  - The layoff-and-search context
Be honest but charitable. These ARE real constraints, not flaws.

## Leadership / operating style
One paragraph (~100 words) + bullet list of named tenets. Draw from
tenet_authored facts directly (list them). Capture the through-line:
org-builder, framework-author, causal-rigor, partner-multiplier.

## Target-role shape
Bullet list organized by tier_opportunity:
  - A-opportunity companies (both A and B tier_company): list them
    with role_type
  - B-opportunity: briefer
Reference the target_company facts directly.

CONFIDENTIALITY & VOICE RULES:
  - Do NOT quote proprietary strategy verbatim from raw docs.
  - Do NOT use identifiable judgments about other people.
  - Voice should sound like the operator's own — specific, confident, not
    generic LinkedIn-speak. Use the Consolidated Prep and Notes.docx
    raw content as voice anchors where possible.
  - If a claim can't be grounded in the evidence, omit it. Do not
    invent accomplishments or frameworks.

EVIDENCE-CITING: for each Track record bullet, Great at bullet, and
Target-role bullet, include a trailing source cue in parens showing
the fact type or archive that supports it — e.g.,
"Base Price Redesign reduced poor pricing 51% vs 5% target (airbnb.md /
major_accomplishment)." Keep these short; they're audit trails, not
distractions.

Output MARKDOWN directly — no JSON wrapper, no code fences around the
whole thing.
"""
