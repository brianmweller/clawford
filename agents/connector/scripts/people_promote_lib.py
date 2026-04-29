"""promote_to_people_brain — shared helper for auto-creating minimal
people/<slug>.md stubs from engagement signals.

Two callers today:

- inbox_triage_lib: when classification returns queued_cold_recruiter,
  promote so the next message in the thread is recognized as `queued`
  via the email_to_slug map (no second recruiter-detector pass needed).
- gmail_sent_mine_lib: when the operator sends to an unmatched recipient,
  promote so future inbounds from the same address triage as `queued`
  instead of `skipped_unknown_sender`.

Both stubs carry an `auto_created: <source>` field so downstream flows
(notably morning-relationship-nudge) can filter them from the
relationship-check-in queue until the operator elevates them. The convention
matches people-expand-from-flux's auto-discovery flag.

Idempotent: a second call for an already-existing slug returns
status=already_exists without modifying the file. the operator's manual edits
are always preserved.
"""
from __future__ import annotations

import re
import sys
from email.utils import parseaddr
from pathlib import Path

for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared import brain  # noqa: E402


def _slugify(text: str) -> str:
    """Lowercase, replace non-alphanumeric runs with single hyphens, strip."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _display_name_from_header(from_header: str, from_email: str) -> str:
    """Extract a quoted display name from a From header. Returns "" when
    the header is bare email or the parsed name equals the email's
    local part (parseaddr returns the local-part as name in that case)."""
    if not from_header:
        return ""
    name, _addr = parseaddr(from_header)
    name = (name or "").strip().strip('"')
    if not name:
        return ""
    if name.lower() == (from_email.partition("@")[0] or "").lower():
        return ""
    return name


def _derive_name_and_slug(from_email: str, from_header: str) -> tuple[str, str]:
    """Return (display_name, slug). When no display name is present,
    incorporate the domain in the slug so jane@a.com and jane@b.com
    don't collide."""
    display = _display_name_from_header(from_header, from_email)
    if display:
        return display, _slugify(display)
    local, _, domain = from_email.partition("@")
    name = local or "unknown"
    slug = _slugify(f"{local}-{domain}") if domain else _slugify(local)
    return name, slug


def promote_to_people_brain(
    *,
    from_email: str,
    from_header: str = "",
    circles: str,
    source: str,
    relationship_type: str = "",
    last_interaction: str = "",
    skip_emails: set[str] | None = None,
    extra_fields: dict[str, str] | None = None,
) -> dict:
    """Create a minimal people/<slug>.md from sender info. Idempotent.

    Args:
        from_email: lowercased before write
        from_header: optional "Display Name <email>" form for richer slug
        circles: comma-joined circle list (e.g. "professional-outer" or
            "recruiter, professional-outer")
        source: short tag landed as `auto_created: <source>` for downstream
            filtering (e.g. "triage_recruiter", "sent_recipient")
        relationship_type: optional, written as `relationship_type:` line
        last_interaction: optional YYYY-MM-DD; written when supplied
        skip_emails: never promote when from_email is in this set (the operator's
            own addresses, prevents self-stub if sent-mine sees a header
            roundtrip)
        extra_fields: any additional `key: value` pairs to write

    Returns:
        {"status": "created"|"already_exists"|"skipped_self",
         "slug": str, "path": str, "name": str}
    """
    email = (from_email or "").strip().lower()
    if not email or "@" not in email:
        return {"status": "skipped_invalid", "slug": "", "path": "", "name": ""}

    if skip_emails and email in {a.lower() for a in skip_emails}:
        return {"status": "skipped_self", "slug": "", "path": "", "name": ""}

    name, slug = _derive_name_and_slug(email, from_header)

    fields: dict[str, str] = {
        "email": email,
        "auto_created": source,
    }
    if relationship_type:
        fields["relationship_type"] = relationship_type
    if last_interaction:
        fields["last_interaction"] = last_interaction
    if extra_fields:
        for k, v in extra_fields.items():
            if v is not None:
                fields[k] = str(v)

    try:
        created = brain.create_person_file(name, circles, slug=slug, **fields)
    except FileExistsError:
        return {
            "status": "already_exists",
            "slug": slug,
            "path": str(brain.dropbox_brain_root() / "people" / f"{slug}.md"),
            "name": name,
        }

    return {
        "status": "created",
        "slug": created["slug"],
        "path": created["path"],
        "name": name,
    }
