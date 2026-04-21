"""Pure helpers for agents/shared/scripts/backfill-known-by.py.

One-time backfill of the `known_by` list on every fact that pre-dates
Phase 5b. Sources that carry reachable participant signal:

  - "Flux numeric" (source_detail is a bare integer → Flux Message.id):
    look up sender/recipient in Flux's SQLite `messages` table.
  - "gmail:<id>" (Gmail-miner-era, Phase 1+): re-fetch the message via
    the Gmail API and extract From/To/Cc headers.

Sources without a participant backref (``LLM-extracted from interaction
data``, ``birthday-miner/...``, krisp meeting titles, workflowy) stay
empty — we choose not to hallucinate membership.

Every resolver maps emails → Clawford slugs via an `email_to_slug`
dict and drops the operator's own addresses. A None return means "couldn't
resolve — skip this fact"; an empty list means "resolved, but nobody
else was on it."
"""
from __future__ import annotations

import json
import re
import sqlite3
from email.utils import getaddresses
from typing import Any, Iterable


_FIELD_RE = re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")


# ─── detect_source_type ──────────────────────────────────────────────


_NUMERIC_RE = re.compile(r"^\d+$")


def detect_source_type(source_detail: str) -> str:
    """Classify a fact's ``source_detail`` into a resolver bucket."""
    if not source_detail:
        return "unknown"
    s = source_detail.strip()
    if not s:
        return "unknown"
    if _NUMERIC_RE.match(s):
        return "flux_numeric"
    if s.startswith("gmail:"):
        return "gmail"
    if s.startswith("workflowy:"):
        return "workflowy"
    if s.startswith("birthday-miner"):
        return "birthday_miner"
    # Krisp meeting-debrief titles don't have a stable prefix — the
    # connector imported them with human-readable labels like
    # "Google Meet with X debrief (event_id ...)". Detect loosely.
    low = s.lower()
    if "debrief" in low or low.startswith("krisp:"):
        return "krisp"
    if "llm-extracted" in low:
        return "llm_extracted"
    return "unknown"


# ─── Flux numeric resolver ───────────────────────────────────────────


def resolve_flux_numeric(
    conn: sqlite3.Connection,
    source_detail: str,
    *,
    email_to_slug: dict[str, str],
    operator_emails: Iterable[str],
) -> list[str] | None:
    """Look up the Flux message by id, map sender + recipient emails to
    Clawford slugs, drop the operator's addresses, and return the slug list
    (sorted, deduped).

    Returns ``None`` when the message id is not found in the Flux DB —
    the caller decides whether to skip or flag. Returns ``[]`` when the
    message exists but every non-the operator address is unknown / null.
    """
    try:
        msg_id = int(source_detail)
    except (TypeError, ValueError):
        return None
    cursor = conn.execute(
        "SELECT sender_address, recipient_address FROM messages WHERE id = ?",
        (msg_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    brian_lower = {str(a).lower() for a in operator_emails}
    slug_set: set[str] = set()
    for addr in row:
        if not addr:
            continue
        lower = str(addr).strip().lower()
        if not lower or lower in brian_lower:
            continue
        slug = email_to_slug.get(lower)
        if slug:
            slug_set.add(slug)
    return sorted(slug_set)


# ─── Gmail resolver ──────────────────────────────────────────────────


def _headers(message: dict) -> list[dict]:
    payload = message.get("payload") or {}
    return payload.get("headers") or []


def _header_emails(headers: list[dict], name: str) -> list[str]:
    """Return all email addresses listed under the given header name.
    Handles comma-separated To/Cc lists via email.utils.getaddresses."""
    raw_values: list[str] = []
    for h in headers:
        if (h.get("name") or "").lower() == name.lower():
            v = h.get("value")
            if v:
                raw_values.append(v)
    out: list[str] = []
    for raw in raw_values:
        for _name, addr in getaddresses([raw]):
            if addr:
                out.append(addr.strip().lower())
    return out


def resolve_gmail(
    service: Any,
    source_detail: str,
    *,
    email_to_slug: dict[str, str],
    operator_emails: Iterable[str],
) -> list[str] | None:
    """Fetch the Gmail message named by ``source_detail`` (``"gmail:<id>"``)
    and return the sorted list of non-the operator slugs found in From/To/Cc.

    Any API error returns ``None`` — the caller can fall through
    without poisoning the rest of the batch.
    """
    msg_id = source_detail[len("gmail:"):] if source_detail.startswith("gmail:") else source_detail
    if not msg_id:
        return None
    try:
        message = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
        ).execute()
    except Exception:  # noqa: BLE001 — degrade-open
        return None

    headers = _headers(message)
    emails = set(_header_emails(headers, "From"))
    emails.update(_header_emails(headers, "To"))
    emails.update(_header_emails(headers, "Cc"))

    brian_lower = {str(a).lower() for a in operator_emails}
    slug_set: set[str] = set()
    for addr in emails:
        if addr in brian_lower:
            continue
        slug = email_to_slug.get(addr)
        if slug:
            slug_set.add(slug)
    return sorted(slug_set)


# ─── Block rewriter ──────────────────────────────────────────────────


def rewrite_block_with_known_by(block: str, known_by: list[str]) -> str:
    """Splice ``- **known_by:** [...]`` into a fact block, after
    audience_scope when that line exists; append at end otherwise.

    Idempotent: a block that already carries ``known_by`` is returned
    unchanged (re-runs of the backfill are safe). An empty list is a
    no-op (nothing worth writing).
    """
    if not known_by:
        return block
    for line in block.splitlines():
        m = _FIELD_RE.match(line)
        if m and m.group(1).strip() == "known_by":
            return block  # already present — leave alone

    new_line = f"- **known_by:** {json.dumps(known_by)}"
    lines = block.splitlines()

    insert_idx = len(lines)
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("- **audience_scope:"):
            insert_idx = i + 1
            break
    lines.insert(insert_idx, new_line)
    return "\n".join(lines)
