"""agents/shared/fuzzy_resolver.py — operator-tool descriptor matching.

Every operator-facing tool that accepts an opaque identifier
(thread_id, event_id, item_id, slug) hits the same UX problem: the
operator doesn't speak opaque ids, they describe the thing
('Michelle's email', 'the board meeting', 'the Q2 roadmap action
item'). Before this module, each tool reinvented the same pattern
around its own candidate pool.

This module factors the pattern out. Callers supply:

  - a list of ``Candidate`` objects (with whichever fields matter
    for that domain — id, display, email, subject, slug, etc.)
  - optional ``id_pattern``: a regex that matches explicit-id
    passthroughs (Gmail thread_ids, GCal event_ids, …)
  - optional ``person_resolver``: a callable that maps a human name
    to a canonical {slug, email, raw} dict — typically
    ``brain.get_person`` with its first-name fallback

The resolver does the rest: case-insensitive substring matching
across the candidate's searchable fields, slug/email bridge via
person_resolver, ranked-ambiguity handling, and recent-candidates
fallback when nothing matches.

Return shape (``ResolutionResult``):

  status="ok"         → id is set, matched carries the Candidate
                         (with a ``match_reason`` string
                         enumerating which signals fired)
  status="ambiguous"  → candidates is set, ordered strong-signal
                         first; the caller should ask the operator
                         to pick one
  status="not_found"  → recent is set with up to ``recent_count``
                         recent candidates for disambiguation help

Shared by: Huckle's reply_to_message + keep_recruiter; Murphy's
force_prep + prep_meeting (Phase 2c rollout).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """A single item in the resolver's match pool.

    Required:
      id:       opaque identifier to return on success
      display:  short label for the ambiguity render path

    Optional searchable fields (any empty field is skipped):
      name:     operator-recognizable person / entity name
      email:    sender / participant email
      subject:  message subject / event title / item description
      slug:     canonical slug (for person_resolver bridging)

    last_seen: ISO timestamp used to rank not_found's recent list
    extras:    caller-defined metadata passed through verbatim
    match_reason: populated by the resolver on successful match
                  (callers rarely set this directly)
    """

    id: str
    display: str = ""
    last_seen: str = ""
    name: str = ""
    email: str = ""
    subject: str = ""
    slug: str = ""
    extras: dict[str, Any] = field(default_factory=dict)
    match_reason: str = ""


@dataclass
class ResolutionResult:
    """Outcome of a resolve_fuzzy_descriptor call. Always set; callers
    branch on ``status`` to decide what to do."""

    status: str                     # "ok" | "ambiguous" | "not_found"
    id: str | None = None            # set when status=ok
    matched: Candidate | None = None  # set when status=ok
    candidates: list[Candidate] | None = None  # set when status=ambiguous
    recent: list[Candidate] | None = None      # set when status=not_found
    reason: str = ""


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------


_EMAIL_IN_TEXT_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)


def _extract_person_emails(person_record: dict | None) -> list[str]:
    """Pull lowercased emails from a person_resolver's return dict.
    Tries both a structured 'email' field (from fields dict) and a
    regex scan of the 'raw' markdown body for people files that store
    multiple addresses."""
    if not person_record:
        return []
    emails: list[str] = []
    direct = (person_record.get("email") or "").strip().lower()
    if direct:
        emails.append(direct)
    # Some person-resolver shapes nest the direct email under 'fields'
    fields_dict = person_record.get("fields") or {}
    nested = (fields_dict.get("email") or "").strip().lower()
    if nested and nested not in emails:
        emails.append(nested)
    raw = person_record.get("raw") or ""
    for match in _EMAIL_IN_TEXT_RE.findall(raw):
        low = match.lower()
        if low not in emails:
            emails.append(low)
    return emails


def resolve_fuzzy_descriptor(
    descriptor: str,
    candidates: Sequence[Candidate],
    *,
    id_pattern: str | None = None,
    person_resolver: Callable[[str], dict | None] | None = None,
    recent_count: int = 5,
) -> ResolutionResult:
    """Match an operator descriptor against a candidate pool.

    descriptor
        Free-form operator input. Anything from an explicit opaque
        id to a person name to a subject substring.
    candidates
        The domain's candidate pool — typically built at call time
        from a log + queue + index.
    id_pattern
        Optional regex. If set and the descriptor matches, the id
        short-circuits past fuzzy matching.
    person_resolver
        Optional callable(name) -> {slug, email, raw, ...} or None.
        When the descriptor resolves to a person, the resolver
        matches the person's slug / email against candidate fields —
        stronger signal than a bare substring.
    recent_count
        How many candidates to return in the ``recent`` list of a
        not_found result (ordered by last_seen desc).
    """
    s = (descriptor or "").strip()
    if not s:
        return ResolutionResult(status="not_found",
                                reason="empty descriptor")

    candidates = list(candidates or [])

    # 1. Explicit-id passthrough — evaluated BEFORE the empty-pool
    #    check so programmatic callers can pass ids the local cache
    #    hasn't populated yet (common during tests + first-run flows
    #    where the queue/log is fresh).
    if id_pattern and re.match(id_pattern, s):
        match = next((c for c in candidates if c.id == s), None)
        if match is None:
            # Bare passthrough candidate — match_reason notes the miss.
            match = Candidate(id=s, display=s,
                              match_reason="id_passthrough_pool_miss")
        else:
            match = _with_reason(match, "id_passthrough")
        return ResolutionResult(status="ok", id=s, matched=match)

    if not candidates:
        return ResolutionResult(status="not_found",
                                reason="no candidates in pool",
                                recent=[])

    # 2. Optional person resolution — bridge name to slug + emails.
    person_slug = ""
    person_emails: list[str] = []
    if person_resolver is not None:
        try:
            record = person_resolver(s)
        except Exception:  # noqa: BLE001 — resolver should not crash us
            record = None
        if record:
            person_slug = (record.get("slug") or "").strip().lower()
            person_emails = _extract_person_emails(record)

    # 3. Score every candidate.
    slow = s.lower()
    is_email_like = "@" in slow
    matches: list[Candidate] = []
    for c in candidates:
        reasons: list[str] = []
        subj = (c.subject or "").lower()
        email = (c.email or "").lower()
        name = (c.name or "").lower()
        slug = (c.slug or "").lower()

        # Strong: person-resolver bridges
        if person_slug and slug == person_slug:
            reasons.append(f"slug={person_slug}")
        if person_emails and email in person_emails:
            reasons.append(f"person_email={email}")

        # Medium: exact email
        if is_email_like and email == slow:
            reasons.append("email_exact")

        # Weak: substrings
        if slow and email and slow in email:
            reasons.append("email_substring")
        if slow and name and slow in name:
            reasons.append("name_substring")
        if slow and subj and slow in subj:
            reasons.append("subject_substring")

        if reasons:
            matches.append(
                _with_reason(c, " + ".join(reasons))
            )

    if not matches:
        recent = sorted(
            candidates, key=lambda c: c.last_seen or "", reverse=True
        )[:recent_count]
        return ResolutionResult(
            status="not_found",
            reason=f"no candidate matches {descriptor!r}",
            recent=recent,
        )

    if len(matches) == 1:
        return ResolutionResult(status="ok",
                                id=matches[0].id,
                                matched=matches[0])

    # Ambiguous — rank strong-signal matches first, then by last_seen.
    def _rank(m: Candidate) -> tuple[int, str]:
        r = m.match_reason or ""
        strong = (
            "slug=" in r
            or "person_email=" in r
            or "email_exact" in r
        )
        # Negate last_seen for descending order via min-sort on tuple;
        # since strings sort lexicographically and iso timestamps are
        # lex-ordered, we invert by using a sentinel for missing.
        return (0 if strong else 1, m.last_seen or "")
    matches.sort(key=lambda m: (_rank(m)[0], -1 * _lex_score(m.last_seen)))
    return ResolutionResult(status="ambiguous", candidates=matches[:8])


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def result_to_dict(
    result: ResolutionResult,
    *,
    id_key: str,
    recent_key: str,
    candidate_formatter: Callable[[Candidate], dict] | None = None,
) -> dict:
    """Translate a ResolutionResult into a standardized dict for tool
    callers. Every domain wrapper should use this so the shape is
    identical across agents:

      {status: "ok",        <id_key>: str, matched: {...}}
      {status: "ambiguous", candidates: [dict, ...]}
      {status: "not_found", reason: str, <recent_key>: [dict, ...]}

    Callers supply the per-domain key names (``"thread_id"`` vs
    ``"meeting_id"`` vs ``"event_id"``) and an optional formatter
    that turns a Candidate into the domain-shaped dict (e.g., pulling
    specific fields out of ``extras``). Without a formatter the
    default shape is ``{<id_key>, subject, email, last_seen,
    match_reason}`` — useful for quick wiring.
    """
    def _default_fmt(c: Candidate) -> dict:
        return {
            id_key: c.id,
            "subject": (c.subject or "")[:80],
            "email": c.email,
            "last_seen": c.last_seen,
            "match_reason": c.match_reason,
        }

    fmt = candidate_formatter or _default_fmt

    if result.status == "ok":
        matched = result.matched
        return {
            "status": "ok",
            id_key: result.id,
            "matched": fmt(matched) if matched else {id_key: result.id},
        }
    if result.status == "ambiguous":
        return {
            "status": "ambiguous",
            "candidates": [fmt(c) for c in (result.candidates or [])],
        }
    return {
        "status": "not_found",
        "reason": result.reason or "no match",
        recent_key: [fmt(c) for c in (result.recent or [])],
    }


def _with_reason(candidate: Candidate, reason: str) -> Candidate:
    """Return a copy of ``candidate`` with ``match_reason`` set. Using
    dataclasses.replace would also work; a shallow construction keeps
    the import footprint small."""
    return Candidate(
        id=candidate.id,
        display=candidate.display,
        last_seen=candidate.last_seen,
        name=candidate.name,
        email=candidate.email,
        subject=candidate.subject,
        slug=candidate.slug,
        extras=dict(candidate.extras or {}),
        match_reason=reason,
    )


def _lex_score(s: str) -> int:
    """Deterministic int from a string for secondary sort tiebreaks.
    ISO timestamps sort fine lexicographically; empty strings rank
    lowest. We map to an int so the final sort key is numeric and
    stable."""
    if not s:
        return 0
    # Just hash to a positive int; exact value doesn't matter, only
    # stability across equal lex prefixes.
    return sum(ord(ch) for ch in s)
