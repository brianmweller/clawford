"""Pure helpers for the Flux → Huckle fact import.

Flux stores facts in SQLite (flux/data/flux.db, knowledge_facts table).
Schema columns relevant to this import:
  id, fact_type, subject, subject_address, content, source_type,
  source_id, audience_scope (JSON), confidence, status, established_at.

Huckle's upsert_fact() consumes:
  subject (slug), category, content, source_agent, source_type,
  source_detail, confidence, idempotency_key, recorded_at,
  audience_scope.

build_email_to_slug_map() builds the email → Huckle-slug lookup so we
can translate Flux's subject_address into Huckle's subject. Facts about
unknown subjects are dropped (caller decides whether to log).

flux_row_to_huckle_fact() does the row-level mapping. Returns None when
the fact should be skipped (unknown subject, empty content, missing
subject_address).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.operator import load_operator  # noqa: E402


_EMAIL_FIELD_RE = re.compile(r"^\s*-\s*\*\*email(?::\*\*|\*\*:)\s*(.+?)\s*$")
_SLUG_FIELD_RE = re.compile(r"^\s*-\s*\*\*slug(?::\*\*|\*\*:)\s*(.+?)\s*$")


def build_email_to_slug_map(people_dir: Path) -> dict[str, str]:
    """Scan every people/*.md (skipping _template.md etc.) and return a
    lowercase email → slug map. People files without an email or with
    placeholder values (em-dash, empty) are skipped."""
    if not people_dir.exists():
        return {}
    out: dict[str, str] = {}
    for path in people_dir.glob("*.md"):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        email = None
        slug = None
        for line in text.splitlines():
            if email is None:
                m = _EMAIL_FIELD_RE.match(line)
                if m:
                    email = m.group(1).strip()
            if slug is None:
                m = _SLUG_FIELD_RE.match(line)
                if m:
                    slug = m.group(1).strip()
            if email is not None and slug is not None:
                break
        if not email or email in {"—", "-", ""} or "@" not in email:
            continue
        if not slug:
            slug = path.stem
        out[email.lower()] = slug
    return out


def parse_audience_scope(raw) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(x) for x in raw]
    s = str(raw).strip()
    if not s or s.lower() == "null":
        return None
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (json.JSONDecodeError, TypeError):
        return None
    return None


def flux_row_to_huckle_fact(row: dict, email_to_slug: dict[str, str]) -> dict | None:
    """Map a Flux knowledge_facts row (dict-like) to the kwargs that
    upsert_fact() accepts. Returns None if the fact should be skipped."""
    subject_address = row.get("subject_address")
    if not subject_address:
        return None
    slug = email_to_slug.get(subject_address.lower())
    if not slug:
        return None
    content = (row.get("content") or "").strip()
    if not content:
        return None

    recorded_at = row.get("established_at") or row.get("created_at") or ""
    if recorded_at and not str(recorded_at).endswith("Z") and "T" in str(recorded_at):
        # established_at sometimes stored without trailing Z; normalize for consistency
        recorded_at = str(recorded_at)

    return {
        "subject": slug,
        "category": row.get("fact_type") or "fact",
        "content": content,
        "source_agent": "flux",
        "source_type": row.get("source_type") or "flux-import",
        "source_detail": row.get("source_id") or "",
        "confidence": float(row.get("confidence") or 0.8),
        "idempotency_key": str(row.get("id")),
        "recorded_at": str(recorded_at),
        "audience_scope": parse_audience_scope(row.get("audience_scope")),
    }


# ---------------------------------------------------------------------------
# People-expansion helpers — used by people-expand-from-flux.py to generate
# minimal Huckle people/*.md stubs for Flux subjects not yet in the brain.
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Name → kebab-case ASCII slug. Strips accents, drops apostrophes,
    collapses whitespace + hyphens, and lowercases."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Drop apostrophes and other internal punctuation without replacing with hyphen
    cleaned = re.sub(r"['`]", "", ascii_only)
    # Replace any run of non-alphanumeric with a single hyphen
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned)
    return cleaned.strip("-").lower()


_SERVICE_LOCAL_EXACT = {
    "no-reply", "noreply", "no_reply", "do-not-reply", "donotreply",
    "notifications", "notification", "notify", "notice",
    "alerts", "alert",
    "mailer", "mailer-daemon", "postmaster", "webmaster", "admin",
    "support", "help", "service", "info", "contact", "hello",
    "billing", "payment", "orders", "order",
    "invoice", "invoices",
    "invitations", "invitation", "invite", "registration",
    "news", "newsletter", "newsletters",
    "hit-reply", "inmail-hit-reply",
    "calendar-notification", "shipment-tracking", "order-update",
}

_SERVICE_LOCAL_PREFIXES = (
    "no-reply", "noreply", "do-not-reply",
    "notifications", "notification",
    "mail-", "mailer-", "news-",
    "order-", "shipment-", "tracking-", "invoice-", "payment-",
    "calendar-", "meeting-", "invite-", "invitations-", "auto-",
    "hit-reply", "inmail-",
    "messages-noreply", "messages-no-reply",  # LinkedIn relay
    "reply-",
)

_SERVICE_DOMAINS = {
    "substack.com",
    "ccsend.com",
    "monarch.com",
    "fidelity.com",
    "stripe.com",
    "stratechery.com",
    "fedex.com",
    "ups.com",
    "usps.com",
    "duck.com",
    "public.govdelivery.com",
    "mail3.guide.co",
    "procaresoftware.com",
    "stanfordhealthcare.org",
    "rivierapartners.com",
    "mail.google.com",
    "diplomatie.gouv.fr",
}

_SERVICE_DOMAIN_SUBSTRINGS = (
    "tracking.", "logistics.", "shipment.", "shipping.",
    "orderstatus.", "order-status.", "deliverystatus.", "delivery-status.",
    "notifications.", "notification.", "noreply.", "donotreply.",
    "mail.fidelity.", "mail.costco.", "mail.amazon.", "mail.stripe.",
)

_SERVICE_NAME_KEYWORDS = (
    "amazon", "google calendar", "linkedin", "dropbox",
    "stripe", "substack", "intercom", "slack app",
    "zoom team", "mailchimp", "hubspot",
    "calendar notification", "calendar reminder",
    "newsletter", "notification",
)

def is_likely_service_account(email: str | None, subject_name: str | None) -> bool:
    """Return True if the (email, name) pair looks like a service account
    or mailing list rather than a human the operator knows personally."""
    if not email:
        return True
    addr = email.lower().strip()
    if "@" not in addr:
        # "dropbox" literal or other bare tokens — not a person
        return True
    if addr in load_operator().emails:
        return True
    local, _, domain = addr.partition("@")
    if local in _SERVICE_LOCAL_EXACT:
        return True
    for prefix in _SERVICE_LOCAL_PREFIXES:
        if local.startswith(prefix):
            return True
    for d in _SERVICE_DOMAINS:
        if domain == d or domain.endswith("." + d):
            return True
    for sub in _SERVICE_DOMAIN_SUBSTRINGS:
        if sub in domain:
            return True
    name_lower = (subject_name or "").lower()
    for kw in _SERVICE_NAME_KEYWORDS:
        if kw in name_lower:
            return True
    return False



_NON_PERSON_FIRST_TOKENS = {
    # Brand names that commonly appear as Flux subject prefixes for
    # non-person topics. Do NOT include brands where the operator has
    # actual human contacts under first names (e.g., OpenAI).
    "apple", "google", "amazon", "fedex", "ups", "usps",
    "chubb", "stripe", "factor", "costco", "netflix",
    "meta", "linkedin", "facebook", "twitter",
    "fidelity", "chase", "monarch", "slack", "zoom",
    "stratechery", "ccsend",
}


_NON_PERSON_NAME_KEYWORDS = (
    "shipment", "shipments", "order", "orders", "tracking",
    "notification", "notifications", "update", "updates",
    "alert", "alerts",
    "archive", "archives", "bash", "event", "events",
    "invoice", "invoices", "receipt", "receipts", "confirmation",
    "interview", "interviews",
    "finances", "finance", "portfolio", "statement", "statements",
    "newsletter", "newsletters", "digest", "weekly", "daily",
    "webinar", "bulletin", "summit", "conference", "fireside",
    "report", "reports",
    "deal", "offer", "promotion",
    "renewal", "subscription", "membership",
    "hours", "project", "delivery", "visa", "camp",
    "prime", "card", "insurance",
    "account", "service", "team", "group", "dept", "department",
    "sales", "marketing", "operations",
    "meeting", "call",
)


def is_self_subject(subject_name: str | None) -> bool:
    return (subject_name or "").lower().strip() in load_operator().name_variants


def is_person_name(name: str | None) -> bool:
    """Heuristic: does `name` look like a human's name rather than a
    topic/event/product/order masquerading as one?"""
    if not name:
        return False
    lower = name.lower()
    for kw in _NON_PERSON_NAME_KEYWORDS:
        if re.search(rf"\b{kw}\b", lower):
            return False
    if any(c.isdigit() for c in name):
        return False
    words = name.split()
    # Need at least first + last — filters "Madonna", brand singletons
    if len(words) < 2 or len(words) > 4:
        return False
    # Every word must start uppercase (filters "AI in sales" — "in" is lowercase)
    for w in words:
        if not w[:1].isupper():
            return False
    # First token can't be a known non-person brand (filters "Apple Vision Pro",
    # "Google Cloud", "Amazon Web Services")
    if words[0].lower() in _NON_PERSON_FIRST_TOKENS:
        return False
    return True


def flux_subject_to_person_stub(
    flux_name: str,
    email: str,
    fact_count: int,
    last_fact_date: str,
) -> dict:
    return {
        "slug": slugify(flux_name),
        "full_name": flux_name,
        "email": (email or "").lower().strip(),
        "fact_count": fact_count,
        "last_fact_date": last_fact_date,
    }


def format_person_md(stub: dict, generated_on: str) -> str:
    return (
        f"# {stub['full_name']}\n"
        f"\n"
        f"- **slug:** {stub['slug']}\n"
        f"- **circles:** unknown\n"
        f"- **relationship_type:** unknown\n"
        f"- **preferred_channel:** email\n"
        f"- **tone:** unknown\n"
        f"- **email:** {stub['email']}\n"
        f"- **phone:** —\n"
        f"- **platforms:** email\n"
        f"- **last_interaction:** {stub.get('last_fact_date') or '—'}\n"
        f"- **auto_generated:** flux-import-{generated_on}\n"
        f"- **notes:** Auto-generated by people-expand-from-flux on {generated_on}. "
        f"Flux subject: \"{stub['full_name']}\", {stub['fact_count']} facts available. "
        f"Refine circles/relationship_type/tone before first draft goes out.\n"
    )
