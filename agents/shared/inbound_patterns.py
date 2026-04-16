"""Regex patterns for inbound-content prompt-injection detection.

Ported from MagicResearch's `web/lib/security/sanitize.ts` — the same
20+ patterns that have been tuned over months of real traffic in that
project (regex list + `validInputs` false-positive corpus in its e2e
test suite). Adapting to Python's `re` module and expanding a couple
of patterns to cover the Clawford surfaces (calendar invites, email
bodies, LinkedIn DMs, news articles, scraped web pages).

Design notes:

- Case-insensitive. All patterns are compiled with `re.IGNORECASE`.
- Biased toward *false negatives* over *false positives*. Per
  MagicResearch's documented tradeoff: "false negatives are much
  less costly than false positives" — a legit calendar invite
  mis-blocked would break the operator's week, whereas a missed injection
  gets caught by the semantic guard and by the propose/confirm gate
  on outbound actions (P0.1).
- Patterns are literal-ish — they target the *shape* of injection
  attempts, not domain-specific phrasing. Normal meeting
  descriptions and product research questions pass.
- Each pattern has a human-readable `label` that surfaces in the
  `flagged_pattern` field of the ScanResult so forensics can see
  *why* a piece of content got quarantined.

See P0.4 in C:/Users/operator/.claude/plans/snug-frolicking-summit.md
and the "Invitation Is All You Need" research for context on why
this module exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InjectionPattern:
    pattern: re.Pattern[str]
    label: str


def _c(src: str, label: str) -> InjectionPattern:
    return InjectionPattern(re.compile(src, re.IGNORECASE), label)


# 20+ patterns covering the main injection classes observed in the wild
# and in current research (Microsoft Prompt Shields, OWASP LLM Top 10,
# "Invitation Is All You Need"). Order does not matter — the first
# match wins only because we short-circuit on hit.

INJECTION_PATTERNS: list[InjectionPattern] = [
    # ── Direct instruction override ───────────────────────────────────
    # Each pattern: trigger verb, then ONE OR MORE qualifiers (your/previous/
    # prior/above/my/the/…) stacked, then the target noun. Stacking handles
    # "disregard the previous instructions", "forget your prior rules",
    # "override the above guidelines" which a single-qualifier regex misses.
    _c(
        r"ignore\s+(?:(?:all|your|previous|prior|above|earlier|preceding|my|the|these|any)\s+)+"
        r"(?:instructions?|prompts?|rules?|context|guidelines?|directives?)",
        "instruction override",
    ),
    _c(
        r"disregard\s+(?:(?:all|your|previous|prior|above|earlier|preceding|my|the|these|any)\s+)+"
        r"(?:instructions?|prompts?|rules?|context|guidelines?)",
        "instruction override",
    ),
    _c(
        r"forget\s+(?:(?:all|your|previous|prior|above|earlier|preceding|my|the|these|any)\s+)+"
        r"(?:instructions?|prompts?|rules?|context|guidelines?)",
        "instruction override",
    ),
    _c(
        r"override\s+(?:(?:all|your|previous|prior|above|earlier|preceding|my|the|these|any)\s+)+"
        r"(?:instructions?|prompts?|rules?|guidelines?|directives?)",
        "instruction override",
    ),
    _c(
        r"do\s+not\s+follow\s+(?:(?:any|all|your|the|these|my|previous|prior|above)\s+)+"
        r"(?:instructions?|prompts?|rules?|guidelines?)",
        "instruction override",
    ),

    # ── Role hijacking ────────────────────────────────────────────────
    _c(r"you\s+are\s+now\s+", "role hijacking"),
    _c(r"act\s+as\s+(if\s+you\s+are|a|an)\s+", "role hijacking"),
    _c(r"pretend\s+(you\s+are|to\s+be)\s+", "role hijacking"),
    _c(r"switch\s+to\s+.{0,20}\s*mode", "role hijacking"),
    _c(
        r"enter\s+(developer|admin|debug|god|sudo|jailbreak|DAN)\s*mode",
        "role hijacking",
    ),

    # ── Prompt / instruction extraction ──────────────────────────────
    _c(
        r"(?:reveal|repeat|show|print|output|display|tell\s+me|"
        r"what\s+(?:is|are|were|would))\s+.{0,20}(?:your|the)\s+.{0,20}"
        r"(?:prompt|instructions?|rules?|configuration|guidelines?|directives?)",
        "prompt extraction",
    ),
    _c(r"system\s*prompt", "prompt extraction"),
    _c(r"what\s+(?:are|were|would\s+be)\s+your\s+instructions", "prompt extraction"),

    # ── Credential / secret extraction ───────────────────────────────
    _c(r"(?:api|secret|access|auth)\s*[\s_-]*key", "secret extraction"),
    _c(
        r"(?:tell|give|show|share|reveal|send)\s+.{0,30}"
        r"(?:password|credentials?|tokens?|secrets?)",
        "secret extraction",
    ),

    # ── Data exfiltration ────────────────────────────────────────────
    # Narrowed from MagicResearch's original `send|post|fetch|curl|wget|
    # exfiltrate` list: `post` and `fetch` false-positive on natural text
    # ("saw your post", "fetch the kids from school"). The remaining verbs
    # are near-unusable in legit prose. `send` stays but requires a
    # following transfer-ish keyword close by.
    _c(r"\b(?:curl|wget|exfiltrate)\b", "data exfiltration"),
    _c(
        r"send\s+(?:the|this|all|your|my|these|any)\s+"
        r"(?:data|secrets?|credentials?|passwords?|tokens?|keys?|emails?|messages?)\s+to\b",
        "data exfiltration",
    ),
    _c(
        r"(?:base64|encode|encrypt)\s+(?:the|your|this|all)",
        "encoding attack",
    ),

    # ── Control-flow manipulation ────────────────────────────────────
    _c(r"\b(?:sudo|admin|root)\b", "privilege escalation"),
    _c(r"\[\s*SYSTEM\s*\]", "fake system message"),
    _c(r"<<\s*(?:SYS|SYSTEM|INST)\s*>>", "fake system tag"),
    _c(r"<\|(?:im_start|im_end|system|endoftext)\|>", "token smuggling"),
    _c(r"<\s*(?:system|instruction|prompt|INST)\s*>", "fake system tag"),
]


def detect_injection(text: str) -> str | None:
    """Return the matched pattern's label, or None if clean.

    Short-circuits on first match. Case-insensitive.
    """
    if not text:
        return None
    for p in INJECTION_PATTERNS:
        if p.pattern.search(text):
            return p.label
    return None
