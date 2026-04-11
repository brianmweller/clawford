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


# ── Signature parsing ───────────────────────────────────────────

_TITLE_PATTERNS = [
    r"(?:^|\n)\s*([A-Z][a-zA-Z\s]+?)\s*(?:\||,|·|–|-)\s*([A-Z][a-zA-Z\s&.]+?)(?:\n|$)",
]
_PHONE_RE = re.compile(r"(\+?\d[\d\s\-().]{7,}\d)")
_LINKEDIN_RE = re.compile(r"(https?://(?:www\.)?linkedin\.com/in/[\w-]+/?)")


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
    for line in sig_lines:
        line = line.strip()
        if not line or len(line) < 5 or len(line) > 100:
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
