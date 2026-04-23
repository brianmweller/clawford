"""agents/shared/recruiter_extract_lib.py — extract company_name +
role_title from an inbound recruiter email.

The output (ExtractedPitch) feeds `company_research.research_company`
so both Huckle (cold-recruiter compose) and Murphy (recruiter-originated
meeting prep) land on the same cache key for the same company.

Strategy:
  1. Mechanical: derive company_domain from the `From:` address.
     If it's an in-house corporate domain (e.g. @anthropic.com) we can
     pre-fill a company-name prior. If it's a third-party ATS domain
     (@lever.co, @greenhouse-mail.io) the domain is useless and the
     body is the only source.
  2. LLM: one terse json_mode call gets the definitive company_name
     and role_title. The domain-derived prior is passed as a hint so
     the model can override it when the body says otherwise.
  3. Cache: results keyed by sha256 of (from_email + subject + body[:500])
     under ~/.clawford/recruiter-extract-cache/. Same inbound twice →
     zero LLM calls.

Degrades gracefully: any LLM failure → ExtractedPitch.error set,
company_domain still populated so Murphy's attendee-domain fallback can
still work. Never raises.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Optional

try:
    from agents.shared.llm import InferResult, infer as _llm_infer_backend
    from agents.shared.recruiter_domains import (
        extract_company_from_ats_domain,
        is_recruiter_domain,
    )
except ImportError:
    from llm import InferResult, infer as _llm_infer_backend  # type: ignore
    from recruiter_domains import (  # type: ignore
        extract_company_from_ats_domain,
        is_recruiter_domain,
    )


DEFAULT_CACHE_DIR = Path(os.path.expanduser("~/.clawford/recruiter-extract-cache"))
LLM_TIMEOUT_S = 20
_BODY_SLICE = 500

# Relay / platform domains whose sender address carries no signal about
# the real hiring company. The actual recruiter is masked behind the
# platform's routing (LinkedIn InMail, etc.). Must fall through to
# body-based LLM extraction. Regression: 2026-04-23 Coinbase prep
# researched LinkedIn because the Gmail thread's From was 'Abby Mintert
# via LinkedIn <hit-reply@linkedin.com>'.
_RELAY_DOMAIN_SUFFIXES: tuple[str, ...] = (
    "linkedin.com",
    "inmail.linkedin.com",
)

_EXTRACT_INSTRUCTIONS = (
    "You extract structured signal from recruiter emails. Reply with "
    "a single valid JSON object. No prose outside the JSON."
)


# ---------------------------------------------------------------------------
# ExtractedPitch
# ---------------------------------------------------------------------------


@dataclass
class ExtractedPitch:
    """Structured extraction from one inbound recruiter email.

    company_name / role_title: the point of the whole exercise — feed
      to company_research. May be "" when the email is too vague.
    company_domain: the literal domain of the `From:` address
      (anthropic.com, lever.co, …). Populated even on LLM failure so
      callers retain a fallback signal.
    confidence: "high" | "medium" | "low" — low when snippets are vague.
    error: set on LLM failure or malformed JSON. ok=False when set.
    """

    company_name: str = ""
    role_title: str = ""
    company_domain: str = ""
    confidence: str = "low"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.company_name)

    def to_prompt_dict(self) -> dict:
        if not self.ok:
            return {}
        return {
            "company_name": self.company_name,
            "role_title": self.role_title,
            "company_domain": self.company_domain,
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# Mechanical helpers
# ---------------------------------------------------------------------------


def _domain_from_email(from_field: str) -> str:
    """Parse an RFC 2822 From value to a bare domain. Empty → ""."""
    if not from_field:
        return ""
    _, addr = parseaddr(from_field)
    if not addr or "@" not in addr:
        return ""
    return addr.split("@", 1)[1].lower().strip()


def _infer_company_from_email(from_field: str) -> Optional[str]:
    """Return a company-name prior derived from the From domain, or
    None when the domain encodes no signal.

    - Direct corporate domain → capitalize the stem ("reddit.com" → "Reddit").
    - In-house ATS subdomain → delegate to extract_company_from_ats_domain.
    - Third-party ATS (lever.co, greenhouse-mail.io) → None.
    - Relay / platform domains (linkedin.com) → None — the real sender
      is masked behind the platform's routing; fall through to body.
    """
    if not from_field:
        return None
    domain = _domain_from_email(from_field)
    if not domain:
        return None
    for relay in _RELAY_DOMAIN_SUFFIXES:
        if domain == relay or domain.endswith("." + relay):
            return None
    if is_recruiter_domain(domain):
        # Could still be an in-house ATS prefix (interview.adobe.com);
        # let extract_company_from_ats_domain decide.
        return extract_company_from_ats_domain(domain)
    # Bare corporate domain: anthropic.com → "Anthropic".
    stem = domain.split(".")[0]
    if not stem:
        return None
    return stem.capitalize()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_key(inbound: dict) -> str:
    """Stable hash of (from_email + subject + body[:500]) — same email
    twice hits the cache; edited emails miss."""
    h = hashlib.sha256()
    from_email = (inbound.get("from_email") or "").strip().lower()
    subject = (inbound.get("subject") or "").strip()
    body = (inbound.get("body") or "")[:_BODY_SLICE]
    h.update(from_email.encode("utf-8"))
    h.update(b"\x00")
    h.update(subject.encode("utf-8"))
    h.update(b"\x00")
    h.update(body.encode("utf-8"))
    return h.hexdigest()[:16]


def _cache_path(key: str, cache_dir: Path) -> Path:
    return cache_dir / f"{key}.json"


def _read_cache(path: Path) -> Optional[ExtractedPitch]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ExtractedPitch(
        company_name=data.get("company_name", "") or "",
        role_title=data.get("role_title", "") or "",
        company_domain=data.get("company_domain", "") or "",
        confidence=data.get("confidence", "low") or "low",
    )


def _write_cache_atomic(path: Path, pitch: ExtractedPitch) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = {
        "company_name": pitch.company_name,
        "role_title": pitch.role_title,
        "company_domain": pitch.company_domain,
        "confidence": pitch.confidence,
        "cached_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def _llm_infer(*, prompt: str, timeout: int = LLM_TIMEOUT_S) -> InferResult:
    return _llm_infer_backend(
        prompt=prompt,
        instructions=_EXTRACT_INSTRUCTIONS,
        json_mode=True,
        timeout=timeout,
    )


def _build_extract_prompt(inbound: dict, domain_prior: str | None) -> str:
    from_line = f"{inbound.get('from_name', '')} <{inbound.get('from_email', '')}>".strip()
    subject = (inbound.get("subject") or "").strip()
    body = (inbound.get("body") or "")[: _BODY_SLICE * 4]  # 2000 chars max
    domain = _domain_from_email(inbound.get("from_email") or "")
    hint_block = ""
    if domain_prior:
        hint_block = (
            f"\nDomain prior (from '{domain}'): the sending domain "
            f"suggests the company is '{domain_prior}'. Override this "
            f"only if the body names a different company explicitly."
        )
    elif domain:
        hint_block = (
            f"\nDomain: '{domain}' is a third-party recruiter platform "
            f"and encodes no company signal. Derive the company from "
            f"the body only."
        )
    return f"""Extract the company and role being pitched in this recruiter email.

FROM: {from_line}
SUBJECT: {subject}
BODY:
{body}
{hint_block}

Return a JSON object with EXACTLY these keys:
  - company_name : the hiring company (e.g. "Anthropic", "Reddit").
                    Empty string if you genuinely cannot tell.
  - role_title   : the specific role (e.g. "Director, Trust & Safety").
                    Empty string if no role is named.
  - confidence   : "high" | "medium" | "low"

Output ONLY the JSON object."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_company_and_role(
    inbound: dict,
    *,
    cache_dir: Path | None = None,
    force: bool = False,
) -> ExtractedPitch:
    """Extract {company_name, role_title, company_domain} from an
    inbound recruiter email.

    Args:
        inbound: dict with keys {from_name, from_email, subject, body}.
        cache_dir: override default ~/.clawford/recruiter-extract-cache.
        force: bypass cache and re-extract.

    Returns:
        ExtractedPitch. Check .ok. Never raises.
    """
    actual_cache_dir = cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR
    key = _cache_key(inbound)
    cache_path = _cache_path(key, actual_cache_dir)

    from_email = inbound.get("from_email") or ""
    company_domain = _domain_from_email(from_email)
    domain_prior = _infer_company_from_email(from_email)

    if not force:
        cached = _read_cache(cache_path)
        if cached is not None:
            # company_domain may have been missing in old cache records;
            # refresh it mechanically without re-running the LLM.
            cached.company_domain = cached.company_domain or company_domain
            return cached

    prompt = _build_extract_prompt(inbound, domain_prior)
    result = _llm_infer(prompt=prompt)
    if not result.ok:
        return ExtractedPitch(
            company_domain=company_domain,
            error=f"llm failure: {result.error}",
        )

    try:
        parsed = json.loads(result.text)
    except (json.JSONDecodeError, ValueError) as e:
        return ExtractedPitch(
            company_domain=company_domain,
            error=f"llm returned invalid JSON: {e}",
        )
    if not isinstance(parsed, dict):
        return ExtractedPitch(
            company_domain=company_domain,
            error="llm returned non-object JSON",
        )

    company_name = (parsed.get("company_name") or "").strip()
    # If the LLM came back empty but we have a reliable domain prior,
    # use it — better something than nothing.
    if not company_name and domain_prior:
        company_name = domain_prior

    pitch = ExtractedPitch(
        company_name=company_name,
        role_title=(parsed.get("role_title") or "").strip(),
        company_domain=company_domain,
        confidence=(parsed.get("confidence") or "low").strip() or "low",
    )

    try:
        _write_cache_atomic(cache_path, pitch)
    except OSError as e:
        print(f"[recruiter_extract] cache write failed: {e}", file=sys.stderr)

    return pitch
