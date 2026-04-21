"""Second-pass redundancy check for Huckle Cat's draft composer.

The first compose pass (compose_lib.build_compose_prompt → LLM) writes
a natural reply that may echo the sender's own message back at them —
the classic "Regarding the Head of Product role at Anthropic," echo
where the role name was already in the inbound. This module is a
second pass that prunes that kind of redundancy.

Two sources of what the recipient already knows:
  1. The inbound message they just sent the operator. Everything in it is
     trivially known — echoing it back is filler.
  2. Facts in the brain where the recipient's slug appears in
     `known_by` (the Phase 5b theory-of-mind tag). These flow through
     as the `recipient_knows=True` subset of the compose context.

Intentional mirroring survives the prune. If the strategy calls for
reinforcing emotional acknowledgment ("glad to hear the news"), the
pruner leaves it alone — the mirror IS the point. The second-pass
prompt surfaces the strategy + recipient_model so the LLM can tell
filler redundancy apart from intentional mirror.

Always-on when the draft has prune surface area (>2 sentences) and a
reply is being composed. Short drafts skip. LLM error / malformed
output / over-aggressive prune all roll back to the original draft —
the pass is a refinement, never a blocker.
"""
from __future__ import annotations

import json
import re
from typing import Callable


_PRUNE_LENGTH_FLOOR = 0.30   # pruned draft must retain ≥30% of chars
_SHORT_DRAFT_SENTENCE_CAP = 2  # skip pass when draft has ≤ this many sentences


_OUTPUT_SCHEMA_HINT = """\
Respond with a JSON object with EXACTLY these fields:
{
  "pruned_draft":        "<the reply with redundant content removed; if nothing is redundant, return the draft unchanged>",
  "removed":             [{"sentence": "<the sentence you pruned, verbatim>",
                           "source":   "inbound | fact:<id>",
                           "reason":   "<one short phrase>"}, ...],
  "preserved_as_mirror": [{"sentence": "<the sentence you KEPT even though it restates known content>",
                           "why":      "<one short phrase — what strategy goal it serves>"}, ...]
}
"""


def _render_facts_block(known_facts: list[dict]) -> str:
    """Render only the recipient_knows=True subset. Mention-slug facts
    are intentionally out of scope — being CC'd on a thread is not the
    same semantic as having received the claim directly."""
    lines: list[str] = []
    for f in known_facts or []:
        if not f.get("recipient_knows"):
            continue
        lines.append(f"  - [{f.get('id', '?')}] {f.get('content', '')}")
    return "\n".join(lines) if lines else "  (none tracked)"


def build_redundancy_check_prompt(
    *,
    draft: str,
    inbound: dict,
    known_facts: list[dict],
    strategy: str,
    recipient_model: str,
) -> str:
    """Assemble the second-pass prompt. Pure — no I/O."""
    facts_block = _render_facts_block(known_facts)
    subject = inbound.get("subject", "")
    body = inbound.get("body", "").strip()

    return f"""You wrote a draft reply on the operator's behalf. Your job now is to
remove content from the draft that is redundant with either:

  (a) the INBOUND MESSAGE the recipient just sent the operator — they already
      know everything in it, so echoing their own words back reads as
      filler; or

  (b) FACTS THEY ALREADY KNOW — prior messages / meetings where this
      recipient was a participant.

Exception — intentional mirroring. If a sentence restates something
they know but the STRATEGY or RECIPIENT MODEL calls for emotional
acknowledgment, warmth, or reinforcement of shared context, KEEP it
and flag it in `preserved_as_mirror`. Examples:
  - a "glad to hear" opener when the strategy says to reinforce warmth
  - a brief "as I mentioned Tuesday" anchor when tying back to prior
    context is load-bearing for the ask
  - any closing beat the recipient_model's emotional outcome depends on

Default is LENIENT — when in doubt, keep the sentence. The pass is a
precision tool for obvious echoes, not a rewrite. You may never change
the meaning of a retained sentence; only whole-sentence removals.

INBOUND MESSAGE THE RECIPIENT SENT
  Subject: {subject}
  Body:
{body if body else "  (empty)"}

FACTS THEY ALREADY KNOW (recipient_knows=True subset)
{facts_block}

STRATEGY FROM THE FIRST-PASS COMPOSITION
  {strategy or "(not provided)"}

RECIPIENT MODEL FROM THE FIRST-PASS COMPOSITION
  {recipient_model or "(not provided)"}

DRAFT TO REFINE
{draft}

{_OUTPUT_SCHEMA_HINT}"""


# ─── Response parsing ────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines)
    return s


def parse_redundancy_result(raw_text: str) -> dict:
    """Validate the LLM's JSON response. Returns either the parsed
    fields or ``{"error": ...}``."""
    cleaned = _strip_fences(raw_text)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return {"error": "malformed json"}
    if not isinstance(parsed, dict):
        return {"error": "not a json object"}

    pruned = parsed.get("pruned_draft")
    if not isinstance(pruned, str):
        return {"error": "missing pruned_draft"}

    removed = parsed.get("removed") or []
    preserved = parsed.get("preserved_as_mirror") or []
    if not isinstance(removed, list):
        removed = []
    if not isinstance(preserved, list):
        preserved = []

    return {
        "pruned_draft": pruned.strip(),
        "removed": [r for r in removed if isinstance(r, dict)],
        "preserved_as_mirror": [p for p in preserved if isinstance(p, dict)],
    }


# ─── Draft shape helpers ─────────────────────────────────────────────


_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]+|[^.!?\n]+$")


def _count_sentences(text: str) -> int:
    """Rough sentence count — used only to decide whether it's worth
    making a second LLM call. Newlines count as sentence boundaries
    so multi-paragraph drafts are recognized."""
    if not text or not text.strip():
        return 0
    # Collapse paragraph breaks to single newlines for the split.
    pieces = _SENTENCE_RE.findall(text)
    return len([p for p in pieces if p.strip()])


# ─── Default LLM caller ──────────────────────────────────────────────


def _default_infer(prompt: str, **kwargs):
    """Lazy-resolve agents.shared.llm.infer so tests don't need the
    HTTP stack imported."""
    from agents.shared.llm import infer
    return infer(prompt=prompt, json_mode=True, **kwargs)


# ─── Public API ──────────────────────────────────────────────────────


def apply_redundancy_check(
    parsed: dict,
    *,
    inbound: dict,
    known_facts: list[dict],
    infer_fn: Callable | None = None,
) -> dict:
    """Run the second-pass prune against ``parsed`` (the output of
    compose_lib.parse_compose_result) in place. Mutates ``parsed``
    with updated draft_text and adds:

      redundancy_status: one of
         "skipped_no_reply" | "skipped_short" | "pruned" | "noop"
         | "llm_error" | "parse_error" | "rejected_over_pruned"
      redundancy: {
         "removed_count": int,
         "preserved_count": int,
         "removed": [{sentence, source, reason}, ...],
         "preserved_as_mirror": [{sentence, why}, ...]
      }  (only when the pass actually ran)

    Never raises on LLM or parse failure — returns parsed unchanged
    with the appropriate status tag so callers can log the outcome.
    """
    if not parsed.get("reply_needed"):
        parsed["redundancy_status"] = "skipped_no_reply"
        return parsed

    draft = str(parsed.get("draft_text") or "")
    if _count_sentences(draft) <= _SHORT_DRAFT_SENTENCE_CAP:
        parsed["redundancy_status"] = "skipped_short"
        return parsed

    caller = infer_fn or _default_infer
    prompt = build_redundancy_check_prompt(
        draft=draft,
        inbound=inbound or {},
        known_facts=known_facts or [],
        strategy=str(parsed.get("strategy") or ""),
        recipient_model=str(parsed.get("recipient_model") or ""),
    )
    result = caller(prompt, json_mode=True)
    if not getattr(result, "ok", False):
        parsed["redundancy_status"] = "llm_error"
        return parsed

    parsed_llm = parse_redundancy_result(getattr(result, "text", "") or "")
    if "error" in parsed_llm:
        parsed["redundancy_status"] = "parse_error"
        return parsed

    pruned = parsed_llm["pruned_draft"]
    removed = parsed_llm["removed"]
    preserved = parsed_llm["preserved_as_mirror"]

    # Guard against over-aggressive pruning. A pruner that collapses
    # the draft to under the length floor is almost certainly wrong;
    # better to ship the original than a gutted reply.
    if not pruned or len(pruned) < max(1, int(len(draft) * _PRUNE_LENGTH_FLOOR)):
        parsed["redundancy_status"] = "rejected_over_pruned"
        return parsed

    status = "noop" if not removed else "pruned"
    parsed["draft_text"] = pruned
    parsed["redundancy_status"] = status
    parsed["redundancy"] = {
        "removed_count": len(removed),
        "preserved_count": len(preserved),
        "removed": removed,
        "preserved_as_mirror": preserved,
    }
    return parsed
