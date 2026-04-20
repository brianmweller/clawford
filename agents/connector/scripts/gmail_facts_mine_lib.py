"""Pure helpers for gmail-facts-mine.py.

Handles cursor I/O, Gmail query construction, candidate-slug derivation,
and message-metadata extraction — all without network or LLM calls so
tests stay fast and deterministic.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path


FALLBACK_AFTER_HOURS = 48


# ---------------------------------------------------------------------------
# Cursor I/O
# ---------------------------------------------------------------------------


def load_cursor(path: Path) -> dict:
    """Return the parsed cursor, or {} if missing/unreadable."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cursor(path: Path, cursor: dict) -> None:
    """Atomic tmp+replace write so a crash mid-write doesn't leave a
    half-serialized cursor behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cursor, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Fallback window logic
# ---------------------------------------------------------------------------


def _parse_iso_utc(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Accept both "2026-04-19T09:00:00Z" and "+00:00" suffix
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def should_use_fallback_window(cursor: dict, *, now_iso: str) -> bool:
    """Return True when we should fall back to a fixed window (e.g. 24h)
    instead of using the cursor. Triggered when the cursor is empty,
    missing last_run_at, or older than FALLBACK_AFTER_HOURS."""
    if not cursor:
        return True
    last_run = _parse_iso_utc(str(cursor.get("last_run_at") or ""))
    if last_run is None:
        return True
    now = _parse_iso_utc(now_iso)
    if now is None:
        return True
    if (now - last_run) > timedelta(hours=FALLBACK_AFTER_HOURS):
        return True
    return False


# ---------------------------------------------------------------------------
# Gmail query
# ---------------------------------------------------------------------------


def build_gmail_query(*, cursor: dict, fallback_days: int, now_iso: str) -> str:
    """Build the Gmail search query. Uses `after:<epoch>` from the cursor
    when we have one; otherwise falls back to `newer_than:Nd`.

    Filters chat messages out and includes both inbox and sent so we
    mine both sides of conversations. -from:me stays intentionally
    off — sent messages about OTHER people's facts are useful too."""
    if should_use_fallback_window(cursor, now_iso=now_iso):
        return f"-in:chats newer_than:{fallback_days}d"

    internal_ms = cursor.get("last_internalDate")
    try:
        epoch_s = int(int(internal_ms) / 1000)
    except (TypeError, ValueError):
        return f"-in:chats newer_than:{fallback_days}d"

    return f"-in:chats after:{epoch_s}"


# ---------------------------------------------------------------------------
# Candidate slug derivation
# ---------------------------------------------------------------------------


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def _split_addrs(header_value: str) -> list[str]:
    if not header_value:
        return []
    out = []
    for part in header_value.split(","):
        _, addr = parseaddr(part)
        if addr:
            out.append(addr.lower())
    return out


def build_candidate_slugs(
    msg: dict,
    *,
    email_to_slug: dict[str, str],
    operator_emails: set[str],
) -> set[str]:
    """Collect the set of known-person slugs referenced by this message's
    From/To/Cc. the operator and unknown senders are filtered out. Returns an
    empty set when no candidate exists — the miner skips LLM invocation
    in that case."""
    operator = {a.lower() for a in operator_emails}
    emails: set[str] = set()

    from_addr = parseaddr(_header(msg, "From"))[1].lower()
    if from_addr:
        emails.add(from_addr)
    for h in ("To", "Cc"):
        emails.update(_split_addrs(_header(msg, h)))

    slugs: set[str] = set()
    for e in emails:
        if e in operator:
            continue
        slug = email_to_slug.get(e)
        if slug:
            slugs.add(slug)
    return slugs


# ---------------------------------------------------------------------------
# Message metadata
# ---------------------------------------------------------------------------


def message_metadata(msg: dict, *, operator_emails: set[str]) -> dict:
    """Return the source_context dict for extract_facts_from_text().
    Includes direction (inbound/outbound) for prompt-hint use."""
    operator = {a.lower() for a in operator_emails}
    from_addr = parseaddr(_header(msg, "From"))[1].lower()
    direction = "outbound" if from_addr in operator else "inbound"
    return {
        "source": "gmail",
        "message_id": msg.get("id", ""),
        "from_email": from_addr,
        "direction": direction,
        "internal_date": msg.get("internalDate", ""),
    }
