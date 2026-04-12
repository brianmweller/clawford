"""
mining_utils.py — Shared utilities for the contact mining pipeline.

Functions extracted from existing agent scripts (gmail-search.py, person-bootstrap.py,
workflowy-sync.py) to avoid duplication across miners.
"""

import json
import os
import re
import sys
from pathlib import Path

# Config path relative to this file
_MINE_DIR = Path(__file__).parent
CONFIG_PATH = _MINE_DIR / "mining-config.json"
CACHE_DIR = _MINE_DIR / "cache"

# ── Config ──────────────────────────────────────────────────────

def load_config():
    """Load mining-config.json."""
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ── Google OAuth ────────────────────────────────────────────────
# Pattern from agents/shopping/scripts/gmail-search.py:63-91

def get_google_credentials(token_path):
    """Load or refresh Google OAuth2 credentials from a token.json file."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not os.path.exists(token_path):
        return None, f"token.json not found at {token_path}"

    with open(token_path) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_data["token"] = creds.token
            with open(token_path, "w") as f:
                json.dump(token_data, f, indent=2)
        except Exception as e:
            return None, f"Token refresh failed: {e}"

    if not creds.valid:
        return None, "Credentials invalid — re-run OAuth flow"

    return creds, None


# ── Email parsing ───────────────────────────────────────────────
# Pattern from agents/meetings-coach/scripts/workflowy-sync.py:332-334

_EMAIL_HEADER_RE = re.compile(r'^\s*"?([^"<]+?)"?\s*<([^>]+)>\s*$')


def parse_email_header(header_value):
    """Parse 'Display Name <email>' into (name, email). Returns (None, None) on failure."""
    if not header_value:
        return None, None
    header_value = header_value.strip()

    match = _EMAIL_HEADER_RE.match(header_value)
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip().lower()
        return name, email

    # Bare email address
    if "@" in header_value and "<" not in header_value:
        email = header_value.strip().lower()
        return None, email

    return None, None


def parse_email_header_list(header_value):
    """Parse a comma-separated list of email headers. Returns list of (name, email) tuples."""
    if not header_value:
        return []
    results = []
    for part in header_value.split(","):
        name, email = parse_email_header(part.strip())
        if email:
            results.append((name, email))
    return results


def normalize_email(email):
    """Normalize email: lowercase, strip whitespace."""
    if not email:
        return None
    return email.strip().lower()


# ── Unified date parsing ───────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date_any(value):
    """Parse a date in any common format. Returns ISO 'YYYY-MM-DD' string or None.

    Handles:
    - ISO:             2024-04-13, 2024-04-13T10:28:53
    - Gmail/email:     'Sep 9, 2024 10:28:53', 'Apr 10, 2025 12:11'
    - Phone export:    '4/10/26 08:10', '8/20/24 18:21' (M/D/YY)
    - US long:         'April 10, 2024'
    """
    if not value:
        return None
    from datetime import datetime
    s = str(value).strip()
    if not s:
        return None

    # ISO prefix (most common from Gmail miner)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # 'Sep 9, 2024' or 'April 10, 2025' (comma required)
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", s)
    if m:
        mon = _MONTH_MAP.get(m.group(1).lower()[:3])
        if mon:
            return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"

    # 'M/D/YY' or 'MM/DD/YYYY' — phone export format
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year = int(m.group(3))
        if year < 100:
            # Two-digit years: 00-49 = 2000s, 50-99 = 1900s
            year += 2000 if year < 50 else 1900
        try:
            datetime(year, month, day)  # validate
            return f"{year:04d}-{month:02d}-{day:02d}"
        except ValueError:
            return None

    # 'D MMM YYYY' (UK-style)
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m:
        mon = _MONTH_MAP.get(m.group(2).lower()[:3])
        if mon:
            return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(1)):02d}"

    return None


# ── Slug generation ─────────────────────────────────────────────
# From agents/meetings-coach/scripts/person-bootstrap.py:42-55

def email_to_slug(email):
    """Convert email to a person slug: jane.doe@example.com -> jane-doe."""
    local = email.split("@")[0]
    slug = re.sub(r"[._]+", "-", local)
    slug = re.sub(r"[^a-z0-9-]", "", slug.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def name_to_slug(name):
    """Convert display name to a person slug: Jane Doe -> jane-doe."""
    slug = re.sub(r"[^a-z0-9\s]", "", name.lower())
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug


# ── Noreply / automated detection ──────────────────────────────

def is_noreply(email, config=None):
    """Check if an email address is an automated/noreply address."""
    if not email:
        return True
    email_lower = email.lower()
    patterns = (config or {}).get("noreply_patterns", [
        "noreply", "no-reply", "notifications", "mailer-daemon",
        "donotreply", "automated", "bounce", "postmaster", "daemon",
    ])
    return any(p in email_lower for p in patterns)


def is_brian(email, config=None):
    """Check if an email address belongs to Sam."""
    if not email:
        return False
    operator_emails = (config or {}).get("operator_emails", [])
    return email.lower() in [e.lower() for e in operator_emails]


# ── Name normalization ──────────────────────────────────────────

def looks_like_real_name(name):
    """A real human name has at least two words starting with uppercase, no digits."""
    if not name:
        return False
    if any(c.isdigit() for c in name):
        return False
    if "@" in name:
        return False
    parts = name.split()
    if len(parts) < 2:
        return False
    return all(p[0].isupper() for p in parts if p)


def looks_like_username(name):
    """Handle/username rather than a real name: lowercase, digits, or no space."""
    if not name:
        return True
    if any(c.isdigit() for c in name):
        return True
    if " " not in name and name == name.lower():
        return True
    return False


def normalize_name(name):
    """Normalize name to 'First Last' format.

    Fixes:
    - 'Last, First' -> 'First Last' (comma-flipped format)
    - 'firstname.lastname' / 'firstname_lastname' / 'firstname-lastname' -> 'Firstname Lastname'
    - Strip honorifics (Dr., Mr., Prof., etc.)
    - Strip parenthetical suffixes: "Name (xWF)" -> "Name"
    - Strip quotes
    - Strip trailing/leading punctuation (comma, period, semicolon)
    - Proper-case ALL CAPS names
    - Reject comma-heavy names (>2 commas = recipient list, not a person)
    """
    if not name:
        return name
    name = name.strip().strip('"').strip("'").strip(",.;:")

    # Recipient list: 3+ comma-separated names = group email. Pick first
    # segment that looks like a real name; otherwise return empty.
    if name.count(",") >= 2:
        segments = [s.strip() for s in name.split(",") if s.strip()]
        real = [s for s in segments if looks_like_real_name(s)]
        if real:
            return real[0]
        # Fall through: no usable name in a recipient list
        return ""

    # Strip parenthetical suffixes at end: "Jiahao Ye (xWF)" -> "Jiahao Ye"
    paren_idx = name.rfind(" (")
    if paren_idx > 0 and name.endswith(")"):
        name = name[:paren_idx]

    # Strip honorifics
    for prefix in ("Dr. ", "Dr ", "Mr. ", "Mr ", "Mrs. ", "Mrs ", "Ms. ", "Ms ",
                   "Prof. ", "Prof ", "Professor "):
        if name.startswith(prefix):
            name = name[len(prefix):]

    # "Last, First" -> "First Last"
    if "," in name:
        parts = [p.strip() for p in name.split(",")]
        if len(parts) == 2 and all(p and not any(c.isdigit() for c in p) for p in parts):
            if parts[0] and parts[1]:
                name = f"{parts[1]} {parts[0]}"

    # Proper-case ALL CAPS (e.g., "JIAHAO YE" -> "Jiahao Ye")
    if name.isupper() and len(name) > 3:
        name = " ".join(w.capitalize() for w in name.split())

    # Handle-style email local parts: firstname.lastname / firstname_lastname
    if (name == name.lower()
            and " " not in name
            and not any(c.isdigit() for c in name)
            and any(sep in name for sep in (".", "_", "-"))):
        parts = re.split(r"[._\-]+", name)
        if 2 <= len(parts) <= 3 and all(p.isalpha() and len(p) >= 2 for p in parts):
            name = " ".join(p.capitalize() for p in parts)

    return name.strip(",.;: ")


# ── Entity classification: is_garbage_entity (hard drop) ─────────
#
# This is one of TWO filtering functions in the entire pipeline. Anything
# matching here is definitely NOT a person and should be dropped at mine time.

# Patterns for bot/reply addresses with opaque tokens in local part
# (GitHub: reply+hash@reply.github.com, Google Calendar: domain_hash@...)
# Matches either:
#  - 20+ contiguous [a-z0-9] after a separator
#  - Local part starting with reply+ / bounce+ / b+ / n+ (common bot prefixes)
_TOKEN_ID_RE = re.compile(r"[_+\-\.][a-z0-9]{20,}@")
_BOT_PREFIX_RE = re.compile(r"^(reply|bounce|bounces|return|feedback-return|unsubscribe|n|b|u)\+[a-z0-9\-_.]{10,}@", re.IGNORECASE)

# Meeting room / facility code patterns: mpk00211f747@fb.com (Meta MPK rooms),
# sun010205d23@fb.com. Pattern: 3-5 letters, 3+ digits, more chars.
_FACILITY_CODE_RE = re.compile(r"^[a-z]{3,5}\d{3,}[a-z0-9]*$")

# Names starting with [BUILDING_CODE] are meeting rooms
_BRACKET_ROOM_RE = re.compile(r"^\[[A-Z0-9.\-]+\]")

# Domains that are always garbage (not real people's mailboxes)
_RESOURCE_DOMAINS = (
    "@resource.calendar.google.com",
    "@group.calendar.google.com",
    "@import.calendar.google.com",
    "@reply.github.com",
    "@noreply.github.com",
    "@notifications.github.com",
    "@rcs.google.com",
)

# Placeholder/junk names
_PLACEHOLDER_NAMES = {
    "unlisted", "unknown", "no name", "(no name)", "none", "null",
    "sender", "recipient",
}

# Role-based local parts — shared mailboxes, not individuals
_ROLE_LOCAL_PARTS = {
    "info", "support", "help", "contact", "contacts", "admin", "hello",
    "sales", "office", "service", "services", "hr", "legal", "compliance",
    "privacy", "security", "noreply", "billing", "accounts", "orders",
    "questions", "inquiries", "feedback", "team", "group", "archives",
    "archive", "shareholder", "shareholders", "privacy-ops", "press",
    "marketing", "newsletter", "updates",
}

# Role suffixes: local parts ending with these are shared mailboxes
_ROLE_SUFFIXES = ("questions", "inquiries", "support", "team", "office", "info", "desk")


def is_garbage_entity(email, name=None, config=None):
    """Is this email+name obviously not a real person?

    Returns (is_garbage: bool, reason: str). Runs at mine time to filter
    data-quality garbage BEFORE it reaches the aggregator.

    Reasons: calendar_resource, bot_token, facility_code, bracket_room,
    placeholder_name, role_address, role_suffix, noreply, newsletter_domain,
    brian_self, no_email.
    """
    if not email:
        return True, "no_email"

    email = email.lower().strip()
    name = (name or "").strip()

    # Sam himself
    if is_brian(email, config):
        return True, "brian_self"

    # Noreply / automated
    if is_noreply(email, config):
        return True, "noreply"

    # Calendar resources, GitHub reply, etc.
    if any(d in email for d in _RESOURCE_DOMAINS):
        return True, "resource_domain"

    # Bot/reply addresses with opaque token
    if _TOKEN_ID_RE.search(email):
        return True, "bot_token"
    if _BOT_PREFIX_RE.match(email):
        return True, "bot_prefix"

    # Meeting room codes like mpk00211f747@fb.com
    local = email.split("@")[0] if "@" in email else email
    if _FACILITY_CODE_RE.match(local):
        return True, "facility_code"

    # Role-based local parts
    if local in _ROLE_LOCAL_PARTS:
        return True, "role_address"
    if any(local.endswith(s) for s in _ROLE_SUFFIXES):
        return True, "role_suffix"

    # Newsletter/mailing list domains
    for d in (config or {}).get("newsletter_domains", []):
        if email.endswith(f"@{d}") or email.endswith(f".{d}"):
            return True, f"newsletter_domain:{d}"

    # Placeholder names
    if name.lower() in _PLACEHOLDER_NAMES:
        return True, "placeholder_name"

    # Bracket-prefixed room names: "[MPK0021.1Z7] Dogster"
    if name and _BRACKET_ROOM_RE.match(name):
        return True, "bracket_room_name"

    return False, ""


# ── Entity classification: is_person (signal gate) ───────────────
#
# Second of TWO filtering functions. Runs at aggregation time AFTER merging
# aliases. Decides if a merged contact has enough signal to be a real
# relationship worth tracking.
#
# A real relationship requires:
# 1. Sam initiated OR had substantial bidirectional interaction, AND
# 2. Some form of real identity (real name, saved GC entry, or messaging)

def is_person(contact, saved_gc_emails=None):
    """Is this merged contact a real person worth a brain entry?

    Returns (is_person: bool, reason: str). Signal-based only — does not
    depend on LLM classification.

    Reasons: no_signal, one_way_no_sent, handle_weak_signal.
    """
    email = (contact.get("email") or "").lower()
    name = (contact.get("name") or "").strip()
    saved_gc_emails = saved_gc_emails or set()

    gmail_sent = contact.get("gmail_sent", 0) or 0
    gmail_recv = contact.get("gmail_received", 0) or 0
    meetings = contact.get("meeting_count", 0) or 0
    krisp = contact.get("krisp_meetings", 0) or 0
    wa = contact.get("whatsapp_messages", 0) or 0
    sms = contact.get("sms_messages", 0) or 0

    total_signal = gmail_sent + gmail_recv + meetings + krisp + wa + sms
    if total_signal == 0:
        return False, "no_signal"

    # Sam never initiated — marketing/institutional
    # (unless there's phone/in-person interaction which means Sam reached out via another channel)
    if gmail_sent == 0 and meetings == 0 and krisp == 0 and wa == 0 and sms == 0:
        return False, "one_way_no_sent"

    # SMS-only tail: 2-3 old messages with someone whose name we don't recognize
    # produces noise. Require ≥5 messages OR a saved GC entry.
    has_other_signal = gmail_sent > 0 or gmail_recv > 0 or meetings > 0 or krisp > 0 or wa > 0
    if not has_other_signal and sms > 0 and sms < 5 and email not in saved_gc_emails:
        return False, "sms_only_tail"

    # Identity signal: real name OR saved Google Contacts OR messaging
    in_saved_gc = email in saved_gc_emails
    has_real_name = looks_like_real_name(name)
    has_name_in_display = any(
        looks_like_real_name(dn) for dn in (contact.get("display_names") or [])
    )
    has_messaging = wa > 0 or sms > 0

    has_identity = in_saved_gc or has_real_name or has_name_in_display or has_messaging

    if not has_identity:
        # Handle name + no trusted identity → require SUBSTANTIAL signal
        # (not just a handful of emails — handles without real names are
        # usually short-lived interactions with strangers).
        substantial = (
            meetings >= 3
            or krisp >= 3
            or (gmail_sent + gmail_recv) >= 10
        )
        if not substantial:
            return False, "handle_weak_signal"

    # Single-word lowercase handle names (arthur, prashant, v) are brain
    # entry noise — they come from Calendar display_names that never got
    # resolved to a real name. Drop unless there's a strong real name
    # in display_names or GC, OR real messaging signal.
    if (name and " " not in name and name == name.lower()
            and not has_name_in_display and not in_saved_gc and not has_messaging):
        return False, "handle_only_name"

    return True, ""


# ── Signature parsing ───────────────────────────────────────────

_TITLE_PATTERNS = [
    r"(?:^|\n)\s*([A-Z][a-zA-Z\s]+?)\s*(?:\||,|·|–|-)\s*([A-Z][a-zA-Z\s&.]+?)(?:\n|$)",
]
_PHONE_RE = re.compile(r"(\+?\d[\d\s\-().]{7,}\d)")
_LINKEDIN_RE = re.compile(r"(https?://(?:www\.)?linkedin\.com/in/[\w-]+/?)")


_SIG_QUOTE_MARKER = re.compile(r"^[\s>\-*<]+|^(on\s+\w{3,}|sent\s+from|date:|from:)", re.IGNORECASE)
_SIG_HAS_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_SIG_VALID_FIELD = re.compile(r"^[A-Z][A-Za-z0-9 .,&'\-/()]{2,60}$")
_SIG_ADDRESS = re.compile(r"\b(p\.?o\.?\s*box|box\s*\d+|,\s*[A-Z]{2}\b|avenue|street|road|boulevard|suite)\b", re.IGNORECASE)
_SIG_BAD_TOKENS = (
    "wrote:", "@", "http", "rights reserved", "unsubscribe",
    "copyright", "all rights", "go to calendar",
)
# Narrative / body-text markers — any of these means it's email prose, not a title
_SIG_NARRATIVE_TOKENS = (
    "i've", "i'm", "i'll", "we've", "we're", "we'll", "you've", "you're",
    "would", "could", "should", "wondering", "wondered", "photos", "some ",
    "just ", "our ", "the ", "here are", "here is", "some of", "just a",
    "if you", "please ", "thanks", "thank you", "sincerely", "cheers",
    "regards", "attached", "fyi", "hey ", "hi ", "hello", "looking forward",
    "let me know", "catching up", "check out", "really ", "such a",
)


def sanitize_signature(sig):
    """Drop junk title/company/phone from a parsed signature dict.

    The cached signatures from Gmail mining frequently contain quote markers,
    email footers, and boilerplate. This filter keeps only values that look
    like real signature fields.
    """
    if not sig:
        return {}
    out = {}

    def clean_text(v):
        if not v:
            return None
        v = str(v).strip()
        if not v or len(v) > 80 or len(v) < 3:
            return None
        lower = v.lower()
        if _SIG_QUOTE_MARKER.match(v):
            return None
        if any(t in lower for t in _SIG_BAD_TOKENS):
            return None
        if any(t in lower for t in _SIG_NARRATIVE_TOKENS):
            return None
        if _SIG_HAS_YEAR.search(v):
            return None
        if _SIG_ADDRESS.search(v):
            return None
        if not _SIG_VALID_FIELD.match(v):
            return None
        # Must be 1-6 words (signature fields are short noun phrases)
        words = v.split()
        if len(words) > 6:
            return None
        return v

    def clean_phone(v):
        if not v:
            return None
        v = str(v).strip()
        if not v or "." in v:
            return None
        digits = "".join(ch for ch in v if ch.isdigit())
        if 10 <= len(digits) <= 15:
            return v
        return None

    t = clean_text(sig.get("title"))
    c = clean_text(sig.get("company"))
    if t:
        out["title"] = t
    if c:
        out["company"] = c
    p = clean_phone(sig.get("phone"))
    if p:
        out["phone"] = p
    li = sig.get("linkedin")
    if li and "linkedin.com" in str(li).lower():
        out["linkedin"] = li
    return out


def parse_signature(body):
    """Extract job title, company, phone, LinkedIn from email signature block.

    Returns dict with keys: title, company, phone, linkedin (any may be None).
    """
    if not body:
        return {}

    result = {}

    # Find signature block: look for common separators near the end
    lines = body.split("\n")
    sig_start = len(lines)

    for i in range(len(lines) - 1, max(len(lines) - 30, -1), -1):
        line = lines[i].strip()
        if line in ("--", "---", "—", "Best,", "Thanks,", "Regards,",
                     "Best regards,", "Cheers,", "Sent from my iPhone",
                     "Sent from my iPad"):
            sig_start = i
            break

    sig_lines = lines[sig_start:] if sig_start < len(lines) else lines[-15:]
    sig_text = "\n".join(sig_lines)

    # Phone
    phone_match = _PHONE_RE.search(sig_text)
    if phone_match:
        result["phone"] = phone_match.group(1).strip()

    # LinkedIn
    li_match = _LINKEDIN_RE.search(sig_text)
    if li_match:
        result["linkedin"] = li_match.group(1)

    # Title and company: look for "Title | Company" or "Title, Company" patterns
    # Skip lines that look like email quote markers or dates.
    _QUOTE_MARKER = re.compile(r"^(on\s+\w+|sent\s+from|-+\s*$|>)", re.IGNORECASE)
    _DATE_LINE = re.compile(r"\b\d{4}\b")  # any 4-digit year
    for line in sig_lines:
        line = line.strip()
        if not line or len(line) < 5 or len(line) > 100:
            continue
        if _QUOTE_MARKER.match(line) or "wrote:" in line.lower() or "@" in line:
            continue
        # "Title | Company" or "Title - Company" or "Title · Company"
        for sep in [" | ", " – ", " - ", " · ", ", "]:
            if sep in line:
                parts = line.split(sep, 1)
                left = parts[0].strip()
                right = parts[1].strip()
                # Heuristic: title is shorter, company is a proper noun
                if 2 < len(left) < 60 and 2 < len(right) < 60:
                    if not any(c.isdigit() for c in left[:5]):
                        # Skip if either side contains a year or @ (likely a date/email quote)
                        if _DATE_LINE.search(line) or "@" in line:
                            continue
                        result.setdefault("title", left)
                        result.setdefault("company", right)
                        break

    return result if result else {}


# ── Checkpoint support ──────────────────────────────────────────

def load_checkpoint(name):
    """Load a checkpoint file from cache. Returns dict or None."""
    path = CACHE_DIR / f"checkpoint-{name}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def save_checkpoint(name, data):
    """Save a checkpoint file to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"checkpoint-{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def save_mined(name, data):
    """Save mined output to cache/mined-{name}.json."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"mined-{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Written: {path}", file=sys.stderr)
