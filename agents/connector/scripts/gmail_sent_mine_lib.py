"""Pure helpers for gmail-sent-mine.py.

Mirrors the shape of gmail_facts_mine_lib.py — cursor I/O, query
construction, and recipient extraction without network or filesystem
I/O except the cursor read/write. Keeps tests fast and deterministic.

The sent-mine is narrower than the fact-mine: it only needs to know
who each sent message was TO, so it can stamp those recipients'
last_interaction. No body parsing, no LLM.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path


FALLBACK_AFTER_HOURS = 48

# Recipient patterns we never want to stamp last_interaction on.
# Sending to no-reply@github.com doesn't mean the operator "interacted" with
# GitHub; sending to a bounce address is a system artifact.
_SKIP_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
)
_SKIP_DOMAIN_SUFFIXES = (
    ".bounces.google.com",
    ".bounce.mailhop.net",
)


# ---------------------------------------------------------------------------
# Cursor I/O
# ---------------------------------------------------------------------------


def load_cursor(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cursor(path: Path, cursor: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cursor, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Fallback window + query construction
# ---------------------------------------------------------------------------


def _parse_iso_utc(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def should_use_fallback_window(cursor: dict, *, now_iso: str) -> bool:
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


def build_gmail_query(*, cursor: dict, fallback_days: int, now_iso: str) -> str:
    """Build the Gmail search query for the sent-mine. `in:sent`
    narrows to the Sent folder; `-in:chats` excludes Google Chat.
    Uses `after:<epoch>` from the cursor when fresh; otherwise a
    fallback window (default 90 days for bootstrap)."""
    if should_use_fallback_window(cursor, now_iso=now_iso):
        return f"in:sent -in:chats newer_than:{fallback_days}d"
    internal_ms = cursor.get("last_internalDate")
    try:
        epoch_s = int(int(internal_ms) / 1000)
    except (TypeError, ValueError):
        return f"in:sent -in:chats newer_than:{fallback_days}d"
    return f"in:sent -in:chats after:{epoch_s}"


# ---------------------------------------------------------------------------
# Recipient extraction + filtering
# ---------------------------------------------------------------------------


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def _split_addrs(header_value: str) -> list[str]:
    if not header_value:
        return []
    out: list[str] = []
    for part in header_value.split(","):
        _, addr = parseaddr(part)
        if addr:
            out.append(addr.lower())
    return out


def _split_addr_pairs(header_value: str) -> list[tuple[str, str]]:
    """Like _split_addrs but preserves the raw `Display Name <email>`
    form for each address, so callers can pass it to
    promote_to_people_brain for richer slug derivation. Returns
    [(email_lower, raw_part_stripped), ...]."""
    if not header_value:
        return []
    out: list[tuple[str, str]] = []
    for part in header_value.split(","):
        raw = part.strip()
        _, addr = parseaddr(part)
        if addr:
            out.append((addr.lower(), raw))
    return out


def is_skippable_recipient(addr: str) -> bool:
    """True for automated / no-reply / bounce addresses. Case-insensitive
    match on local-part prefix and domain suffix."""
    if not addr or "@" not in addr:
        return True
    local, _, domain = addr.lower().partition("@")
    for p in _SKIP_LOCAL_PREFIXES:
        if local == p or local.startswith(p + "-") or local.startswith(p + "."):
            return True
    for s in _SKIP_DOMAIN_SUFFIXES:
        if domain.endswith(s):
            return True
    return False


def extract_recipient_emails(
    msg: dict,
    *,
    operator_emails: set[str],
) -> set[str]:
    """Collect To + Cc email addresses for a sent message, filtering
    out the operator's own addresses (replies-to-self edge case) and
    noreply/bounce automated recipients.

    Bcc is intentionally omitted: Gmail's own Sent folder doesn't
    reliably carry Bcc headers (varies by client and Google's own
    rewriting), so relying on them is brittle. If a future contract
    needs Bcc, resolve via the rawContent format rather than headers."""
    operator = {a.lower() for a in operator_emails}
    out: set[str] = set()
    for hdr in ("To", "Cc"):
        for addr in _split_addrs(_header(msg, hdr)):
            if addr in operator:
                continue
            if is_skippable_recipient(addr):
                continue
            out.add(addr)
    return out


def extract_recipient_pairs(
    msg: dict,
    *,
    operator_emails: set[str],
) -> list[tuple[str, str]]:
    """Like extract_recipient_emails but yields (email, raw_header)
    tuples so auto-promote can derive a clean slug from the display
    name. Same filtering semantics. Order: To addresses first, then
    Cc, deduped on email (first occurrence wins)."""
    operator = {a.lower() for a in operator_emails}
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for hdr in ("To", "Cc"):
        for addr, raw in _split_addr_pairs(_header(msg, hdr)):
            if addr in operator:
                continue
            if is_skippable_recipient(addr):
                continue
            if addr in seen:
                continue
            seen.add(addr)
            out.append((addr, raw))
    return out


# ---------------------------------------------------------------------------
# Date conversion
# ---------------------------------------------------------------------------


def internal_date_to_iso_date(internal_ms: str | int | None) -> str | None:
    """Gmail `internalDate` is epoch milliseconds as a string.
    Convert to YYYY-MM-DD (UTC). Returns None on parse failure."""
    if internal_ms is None or internal_ms == "":
        return None
    try:
        ms = int(internal_ms)
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.date().isoformat()
