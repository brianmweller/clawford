"""Pure helpers for gmail-facts-mine.py.

Handles cursor I/O, Gmail query construction, candidate-slug derivation,
and message-metadata extraction — all without network or LLM calls so
tests stay fast and deterministic.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path


FALLBACK_AFTER_HOURS = 48


# ---------------------------------------------------------------------------
# Person-file field parsing (shared across helpers in this module)
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^\s*-\s*\*\*slug(?::\*\*|\*\*:)\s*(.+?)\s*$")
_PARENT_SLUG_RE = re.compile(r"^\s*-\s*\*\*parent_slug(?::\*\*|\*\*:)\s*(.+?)\s*$")
_FULL_NAME_RE = re.compile(r"^\s*-\s*\*\*full_name(?::\*\*|\*\*:)\s*(.+?)\s*$")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")


def build_parent_to_children_map(people_dir: Path) -> dict[str, list[str]]:
    """Scan people/*.md for `parent_slug:` references and return a
    {parent_slug → [child_slug, ...]} map.

    Placeholder values (em-dash, hyphen, empty) and self-cycles are
    silently dropped. Files starting with an underscore (templates,
    archives) are skipped. Missing directory returns {}.

    v1 limitations: single parent only, depth 1 (no grandparent
    expansion by callers), no multi-parent / step-parent support.
    """
    if not people_dir.exists():
        return {}
    out: dict[str, list[str]] = {}
    for path in sorted(people_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        slug: str | None = None
        parent: str | None = None
        for line in text.splitlines():
            if slug is None:
                m = _SLUG_RE.match(line)
                if m:
                    slug = m.group(1).strip()
            if parent is None:
                m = _PARENT_SLUG_RE.match(line)
                if m:
                    parent = m.group(1).strip()
            if slug is not None and parent is not None:
                break
        if not slug or not parent:
            continue
        if parent in {"—", "-", ""}:
            continue
        if parent == slug:
            continue  # self-cycle guard
        out.setdefault(parent, []).append(slug)
    return out


def build_mention_candidate_slugs(
    *,
    primary: set[str],
    parent_to_children: dict[str, list[str]],
) -> set[str]:
    """Given the primary (addressed) candidate slugs and a parent-to-children
    map, return the set of mention-candidate slugs: children of any primary,
    excluding slugs that are themselves in primary.

    Depth 1 only — no transitive walk (Jamie → Arthur, not Jamie → Arthur →
    Arthur's-eventual-kid). That's a deliberate v1 constraint; extending to
    depth N wants cycle detection the simple self-cycle guard doesn't cover.
    """
    mention: set[str] = set()
    for p in primary:
        for child in parent_to_children.get(p, []):
            if child not in primary:
                mention.add(child)
    return mention


def build_person_info_map(people_dir: Path) -> dict[str, dict]:
    """Scan people/*.md and return slug → {full_name, first_name}.
    full_name defaults to the file's H1 if no explicit field; first_name
    is derived as the first whitespace-separated token of full_name."""
    if not people_dir.exists():
        return {}
    out: dict[str, dict] = {}
    for path in sorted(people_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        slug: str | None = None
        full_name: str | None = None
        h1: str | None = None
        for line in text.splitlines():
            if slug is None:
                m = _SLUG_RE.match(line)
                if m:
                    slug = m.group(1).strip()
            if full_name is None:
                m = _FULL_NAME_RE.match(line)
                if m:
                    full_name = m.group(1).strip()
            if h1 is None:
                m = _H1_RE.match(line)
                if m:
                    h1 = m.group(1).strip()
            if slug and full_name:
                break
        if not slug:
            slug = path.stem
        name = full_name or h1 or slug
        first = name.split()[0] if name else ""
        out[slug] = {"full_name": name, "first_name": first}
    return out


def filter_mention_info(
    mention_slugs: set[str],
    info_map: dict[str, dict],
) -> dict[str, dict]:
    """Narrow a full person-info map to just the slugs in `mention_slugs`.
    Missing slugs are omitted (caller gets a closed set containing only
    slugs we have names for)."""
    return {s: info_map[s] for s in mention_slugs if s in info_map}


def filter_rejected_facts(
    facts: list[dict],
    skip_signatures: set[tuple[str, str]],
) -> tuple[list[dict], list[dict]]:
    """Partition `facts` into (kept, dropped) where dropped = those whose
    (subject, content_hash) signature appears in `skip_signatures`.

    Skip-signatures are produced by pending_review_resolve.signature_for()
    and populated at miner startup via load_rejected_signatures(). Lets
    operator rejections from _rejected.md suppress re-proposals on the
    next mining pass without needing a schema join between the two files.
    """
    if not skip_signatures:
        return list(facts), []
    # Lazy import so the lib stays importable without the shared package
    # being on sys.path (same convenience pattern used elsewhere here).
    try:
        from pending_review_resolve import signature_for  # type: ignore
    except ImportError:
        from agents.shared.pending_review_resolve import signature_for  # type: ignore

    kept: list[dict] = []
    dropped: list[dict] = []
    for f in facts:
        sig = signature_for(
            subject=str(f.get("subject", "")),
            content=str(f.get("content", "")),
        )
        if sig in skip_signatures:
            dropped.append(f)
        else:
            kept.append(f)
    return kept, dropped


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
