"""Cross-reference a recruiter's full name + email by searching Gmail.

A calendar invite description often ships with only a sign-off first
name ('Best,\\n\\nAbby') — the recruiter's last name and email address
live in the separate email thread that introduced the meeting. This
module searches the authenticated user's Gmail for messages that share
a strong signal with the calendar event (Google Meet URL, company
token, first-name hint), and returns the first non-operator sender's
parsed {name, email}.

Usage:
    from agents.shared.gmail_recruiter_lookup import resolve_recruiter_from_gmail
    result = resolve_recruiter_from_gmail(
        service=gmail_service,
        event=event,
        first_name_hint="Abby",
        operator_emails={"sam.smith@example.com"},
    )
    # result: {"name": "Abby Mintert", "email": "abby@coinbase.com"} or None

Search precedence (strongest signal first):
  1. Google Meet URL scraped from event.description — unique identifier.
  2. First-name + company token (from event title) together.
  3. First-name alone — weakest, run only when days_back is small.

Only the From: header is parsed; To / Cc / Bcc are ignored. Messages
where the From: address belongs to the operator (operator_emails) are
skipped so we don't pull the operator's own forwarded notes.
"""
from __future__ import annotations

import re
from email.utils import parseaddr


_MEET_URL_RE = re.compile(
    r"https?://meet\.google\.com/[a-z0-9\-]+", re.IGNORECASE,
)


def _extract_meet_url(event: dict) -> str:
    """Return the first Google Meet URL found in event.description, or
    the event.conference_link / event.hangoutLink field, or ''."""
    for key in ("conference_link", "hangoutLink"):
        val = (event.get(key) or "").strip()
        if val:
            return val
    desc = event.get("description") or ""
    m = _MEET_URL_RE.search(desc)
    return m.group(0) if m else ""


def _extract_company_token(event: dict) -> str:
    """Pull a single likely company token from the event title. Mirrors
    workflowy-sync's _extract_company_from_event_title heuristic — uses
    'with X' and 'X / Y' patterns. Empty string on no match."""
    title = (event.get("summary") or event.get("title") or "").strip()
    if not title:
        return ""
    m = re.search(r"\bwith\s+([A-Z][A-Za-z0-9&]+)\b", title)
    if m:
        return m.group(1)
    m = re.match(r"^([A-Z][A-Za-z0-9&]+)\s*(?:/|@)\s*\w+", title)
    if m:
        return m.group(1)
    return ""


def _gmail_search(service, q: str, max_results: int = 10) -> list[str]:
    """Return Gmail message ids matching the query, newest-first. Thin
    wrapper so callers can swap in a stub in tests."""
    resp = service.users().messages().list(
        userId="me", q=q, maxResults=max_results,
    ).execute()
    return [m["id"] for m in resp.get("messages", []) or []]


def _fetch_from_header(service, message_id: str) -> str:
    """Fetch just the From: header for a message id. Uses metadata
    format to avoid pulling the whole body."""
    msg = service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From"],
    ).execute()
    for h in msg.get("payload", {}).get("headers", []) or []:
        if h.get("name", "").lower() == "from":
            return h.get("value", "") or ""
    return ""


def _parse_from_header(header: str) -> tuple[str, str]:
    """Return (display_name, email) from a From: header. display_name
    comes back stripped of surrounding whitespace and quote chars."""
    name, email = parseaddr(header or "")
    name = (name or "").strip().strip('"').strip("'")
    email = (email or "").strip().lower()
    return name, email


def _name_matches_first(name: str, first_name_hint: str) -> bool:
    """True when `name` starts with `first_name_hint` (case-insensitive).
    Examples: 'Abby Mintert' matches 'Abby'; 'Abigail Mintert' doesn't.
    Guards against the hint being a common English word ('the operator' already
    filtered upstream via operator_emails, but 'The Recruiter' style
    noise needs the explicit word-boundary check)."""
    if not name or not first_name_hint:
        return False
    hint = first_name_hint.strip().lower()
    if not hint:
        return False
    first_token = name.strip().split(" ", 1)[0].lower()
    return first_token == hint


def _best_sender_match(
    service,
    message_ids: list[str],
    first_name_hint: str,
    operator_emails: set[str],
) -> dict | None:
    """Walk message ids newest-first, return the first non-the operator sender
    whose display-name first token matches the hint. Stops as soon as
    one matches so we don't iterate the entire window."""
    brian_lc = {a.strip().lower() for a in (operator_emails or set()) if a}
    for mid in message_ids:
        header = _fetch_from_header(service, mid)
        name, email = _parse_from_header(header)
        if email in brian_lc:
            continue
        if not name:
            # A bare address ('abby@coinbase.com' with no display name)
            # can still be useful if the local-part matches the hint —
            # but we can't recover a LAST name from it, so skip.
            continue
        if _name_matches_first(name, first_name_hint):
            return {"name": name, "email": email}
    return None


def resolve_recruiter_from_gmail(
    service,
    event: dict,
    first_name_hint: str,
    operator_emails: set[str] | None = None,
    days_back: int = 30,
) -> dict | None:
    """Cross-reference the recruiter's full name by searching Gmail.

    Returns ``{"name": "Abby Mintert", "email": "abby@coinbase.com"}``
    on hit, or ``None`` when no matching sender is found within the
    search window.

    Three query tiers, run in order until one yields a match:
      1. Google Meet URL verbatim (strongest — unique per invite).
      2. First-name hint + company token (e.g. 'Abby' + 'Coinbase').
      3. First-name hint alone, newer_than:{days_back}d.

    Empty ``first_name_hint`` short-circuits to None immediately — there
    is nothing to match against, and a bare company search would pull
    unrelated threads.
    """
    hint = (first_name_hint or "").strip()
    if not hint:
        return None

    operator_emails = operator_emails or set()

    queries: list[str] = []

    meet_url = _extract_meet_url(event)
    if meet_url:
        queries.append(f'"{meet_url}"')

    company = _extract_company_token(event)
    if company:
        queries.append(
            f'"{hint}" "{company}" newer_than:{days_back}d'
        )

    queries.append(f'"{hint}" newer_than:{days_back}d')

    for q in queries:
        try:
            ids = _gmail_search(service, q, max_results=10)
        except Exception:  # noqa: BLE001 — Gmail API is best-effort
            continue
        if not ids:
            continue
        match = _best_sender_match(service, ids, hint, operator_emails)
        if match:
            return match

    return None
