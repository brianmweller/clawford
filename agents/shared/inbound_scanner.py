"""Inbound-content scanner — indirect prompt-injection defense.

This is the *mirror* of the outbound reviewer (P0.1). Every script that
feeds external text into an LLM prompt should pass that text through
`scan_inbound()` first and only feed the returned `wrapped_text` into
the prompt. System prompts of ingesting scripts should include
`ANTI_LEAKAGE_SUFFIX`.

Why this exists
---------------

The research corpus of 2025–2026 (Microsoft Prompt Shields, the
"Invitation Is All You Need" paper, the Gemini/Google-Calendar
exploit) shows that a malicious calendar invite, email, or LinkedIn
message can inject instructions the agent then executes. Clawford has
several undefended inbound surfaces: Calendar reads (Murphy, Mistress
Mouse), LinkedIn and news ingest (Lowly Worm), Gmail reads (Huckle
Cat), Telegram inbound, web-scraped product pages (Hilda).

Defense shape (three layers, borrowed verbatim from MagicResearch):

1. **Regex layer** (`inbound_patterns.py`) — deterministic pattern
   match for instruction-override, role-hijacking, prompt extraction,
   secret extraction, exfiltration, encoding, fake system tags, token
   smuggling. Hard-block on match.
2. **Semantic guard** (optional) — LLM classifier that runs in
   parallel with the ingest path, returns SAFE / UNSAFE. Explicit
   bias toward SAFE. Fails open on LLM error.
3. **Input wrapping** — accepted text is wrapped in
   `<untrusted-data source="...">...</untrusted-data>` tags before
   being interpolated into any prompt, and every ingesting script's
   system prompt is suffixed with `ANTI_LEAKAGE_SUFFIX`.

Usage
-----

    from agents.shared.inbound_scanner import scan_inbound, ANTI_LEAKAGE_SUFFIX

    result = scan_inbound(
        text=event["description"],
        source_type="calendar",
        source_id=event["id"],
    )
    if result.status == "block":
        return {"status": "degraded",
                "alert": f"Inbound-scan blocked calendar event {event['id']}: "
                         f"{result.flagged_pattern}",
                "scan_result": result.as_dict()}
    # else: feed result.wrapped_text into your LLM prompt

See P0.4 in C:/Users/operator/.claude/plans/snug-frolicking-summit.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from inbound_patterns import detect_injection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard upper bound per field. MagicResearch uses 10K chars; Clawford
# ingests full news articles and meeting transcripts that can be
# larger, so we bump to 50K. Anything above this is almost certainly
# a parser producing junk rather than real content.
MAX_INPUT_LENGTH = 50_000

# Envelope-level cap: if an ingest blob has more than this many
# distinct fields, something's wrong upstream.
MAX_FIELD_COUNT = 40

# Prompt template paths (loaded lazily so tests can override via env var).
PROMPTS_DIR_ENV_VAR = "CLAWFORD_PROMPTS_DIR"
_DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _prompts_dir() -> Path:
    raw = os.environ.get(PROMPTS_DIR_ENV_VAR)
    if raw:
        return Path(raw)
    return _DEFAULT_PROMPTS_DIR


def _read_prompt(name: str) -> str:
    p = _prompts_dir() / name
    return p.read_text(encoding="utf-8").strip()


def _anti_leakage() -> str:
    return _read_prompt("anti_leakage.txt")


def _semantic_guard_system() -> str:
    return _read_prompt("semantic_guard.txt")


# Public constant for ingesting scripts. Resolved at call-time so a
# test that overrides CLAWFORD_PROMPTS_DIR sees the override.
def anti_leakage_suffix() -> str:
    """System-prompt suffix every ingesting script should append."""
    return _anti_leakage()


# ---------------------------------------------------------------------------
# Scan result
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """Outcome of a scan_inbound() call.

    Attributes:
        status: "allow" | "quarantine" | "block".
          - "allow": text is clean, use `wrapped_text` freely.
          - "quarantine": text tripped a soft signal (e.g., length
            limit, or a weak pattern in warn-only mode). The caller
            decides whether to use it — most callers should either
            skip or route through human review.
          - "block": text tripped a hard signal (regex hit or
            semantic guard returned UNSAFE). Do not feed into any
            LLM prompt.
        flagged_pattern: Human-readable label of the pattern that
            triggered a block, or None if the scan allowed.
        reason: One-line explanation suitable for an alert / log.
        wrapped_text: Text wrapped in <untrusted-data> tags, ready
            to interpolate into a prompt. Empty string when blocked.
        source_type: Echoed from the call.
        source_id: Echoed from the call.
    """

    status: str
    flagged_pattern: Optional[str] = None
    reason: str = ""
    wrapped_text: str = ""
    source_type: str = ""
    source_id: str = ""
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "flagged_pattern": self.flagged_pattern,
            "reason": self.reason,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "extras": self.extras,
        }

    @property
    def allowed(self) -> bool:
        return self.status == "allow"


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------


def wrap_untrusted(text: str, source_type: str, source_id: str = "") -> str:
    """Wrap `text` in <untrusted-data> delimiters for prompt interpolation.

    Any `>` in the source_type/source_id is stripped to keep the
    delimiter shape unambiguous. `text` itself is not escaped — the
    anti-leakage suffix tells the model to treat tag-internal content
    as data, not instructions. Escaping here would break scripts that
    expect to see the original content verbatim in the LLM context.
    """
    safe_type = (source_type or "unknown").replace(">", "").replace("\n", " ")
    safe_id = (source_id or "").replace(">", "").replace("\n", " ")
    attr = f'source="{safe_type}"'
    if safe_id:
        attr += f' id="{safe_id}"'
    return f"<untrusted-data {attr}>\n{text}\n</untrusted-data>"


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def scan_inbound(
    text: str,
    source_type: str,
    source_id: str = "",
    *,
    max_length: int = MAX_INPUT_LENGTH,
) -> ScanResult:
    """Run regex layer against `text`, return a ScanResult.

    This is the fast-path scan — no LLM calls, deterministic, pure.
    For LLM-based semantic classification, call `semantic_guard()`
    separately (typically in parallel with the main ingest call).

    A length overflow is a `quarantine`, not a `block` — the caller
    may still want to salvage a truncated version. A regex hit is a
    `block`.
    """
    common = {"source_type": source_type, "source_id": source_id}

    if text is None:
        return ScanResult(
            status="allow",
            reason="empty input",
            wrapped_text=wrap_untrusted("", source_type, source_id),
            **common,
        )

    if len(text) > max_length:
        return ScanResult(
            status="quarantine",
            reason=f"input length {len(text)} exceeds max {max_length}",
            wrapped_text="",
            extras={"length": len(text), "max_length": max_length},
            **common,
        )

    injection = detect_injection(text)
    if injection:
        return ScanResult(
            status="block",
            flagged_pattern=injection,
            reason=f"regex pattern matched: {injection}",
            wrapped_text="",
            **common,
        )

    return ScanResult(
        status="allow",
        wrapped_text=wrap_untrusted(text, source_type, source_id),
        **common,
    )


# ---------------------------------------------------------------------------
# Semantic guard (optional LLM classifier)
# ---------------------------------------------------------------------------


@dataclass
class GuardResult:
    """Outcome of a semantic_guard() call.

    safe: True if the classifier said SAFE or if the classifier
        failed (fail-open). False only on a concrete UNSAFE verdict.
    verdict: "safe" | "unsafe" | "error".
    reason: One-line explanation when unsafe; empty when safe.
    trace_id: Propagated from the underlying llm.infer() call.
    """

    safe: bool
    verdict: str
    reason: str = ""
    trace_id: str = ""


def semantic_guard(
    text: str,
    *,
    source_type: str = "unknown",
    source_id: str = "",
    trace_id: str | None = None,
    timeout: int = 15,
    infer_fn=None,
) -> GuardResult:
    """LLM-based classifier for novel / paraphrased injection.

    Returns SAFE by default on any error — per MagicResearch's
    documented bias, a missed injection gets caught by the outbound
    reviewer (P0.1) and the rate limiter (P1.3), whereas blocking
    a legit message breaks the operator's week.

    Runs cheap: 10-token max output, terse system prompt, one call.
    Designed to be called in parallel with the main ingest path
    (asyncio.gather-style) so it doesn't add latency on the happy
    path — but this module is sync-only; callers wanting parallelism
    should spawn a thread or use the async wrapper in whatever their
    script layer is.

    `infer_fn` is injected for tests. When not provided, imports
    `llm.infer` lazily so this module can be imported without
    initializing the LLM stack.
    """
    if not text or not text.strip():
        return GuardResult(safe=True, verdict="safe", trace_id=trace_id or "")

    if infer_fn is None:
        # Lazy import so unit tests that only exercise the regex layer
        # don't need the LLM stubs.
        try:
            from llm import infer as infer_fn  # type: ignore
        except Exception as e:
            return GuardResult(
                safe=True,  # fail open
                verdict="error",
                reason=f"llm.infer import failed: {e}",
                trace_id=trace_id or "",
            )

    try:
        system = _semantic_guard_system()
    except FileNotFoundError as e:
        return GuardResult(
            safe=True,
            verdict="error",
            reason=f"semantic_guard.txt missing: {e}",
            trace_id=trace_id or "",
        )

    user_prompt = (
        f"Classify this {source_type} content (source id: {source_id or 'n/a'}):\n\n"
        f"---\n{text}\n---"
    )

    result = infer_fn(
        user_prompt,
        instructions=system,
        timeout=timeout,
        trace_id=trace_id,
    )

    if not getattr(result, "ok", False):
        return GuardResult(
            safe=True,  # fail open on LLM error
            verdict="error",
            reason=getattr(result, "error", "") or "llm call failed",
            trace_id=getattr(result, "trace_id", "") or (trace_id or ""),
        )

    verdict_text = (getattr(result, "text", "") or "").strip().lower()
    # Accept "safe", "unsafe", or a short sentence that contains either word.
    if "unsafe" in verdict_text:
        return GuardResult(
            safe=False,
            verdict="unsafe",
            reason=f"semantic guard flagged {source_type}:{source_id} — verdict={verdict_text!r}",
            trace_id=getattr(result, "trace_id", "") or (trace_id or ""),
        )
    if "safe" in verdict_text:
        return GuardResult(
            safe=True,
            verdict="safe",
            trace_id=getattr(result, "trace_id", "") or (trace_id or ""),
        )
    # Ambiguous verdict — treat as safe (fail open), but log the shape.
    return GuardResult(
        safe=True,
        verdict="error",
        reason=f"ambiguous guard verdict: {verdict_text!r}",
        trace_id=getattr(result, "trace_id", "") or (trace_id or ""),
    )
