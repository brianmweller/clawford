"""recruiter_detector_lib — classify inbound email as likely recruiter
outreach.

Used by Huckle's triage to route cold recruiter inbounds (senders not
in the operator's people directory) into the drafting pipeline instead of
silently skipping them as 'unknown_sender'. Returns a confidence
score + signal attribution so Telegram FYIs and audit logs can
explain WHY a message was flagged.

Pure function. No network, no filesystem. Tested with fixtures.
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# ATS / recruiting platform domains (HIGH confidence signal)
# ---------------------------------------------------------------------------


# Domains used by applicant-tracking systems and recruiting platforms.
# Match is done by suffix (e.g., ends with these), so subdomain variants
# like "hire.lever.co" or "mail.greenhouse-mail.io" also match.
RECRUITER_DOMAINS: set[str] = {
    "greenhouse-mail.io",
    "greenhousemail.io",
    "lever.co",
    "ashbyhq.com",
    "hire.withgoogle.com",
    "applytojob.com",
    "workable.com",
    "smartrecruiters.com",
    "breezy.hr",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
    "notion.so",   # Notion careers emails; risky but acceptable
    "mail.notion.so",
    "mail.hire.com",
    "rivierapartners.com",   # retained exec search
    "russellreynolds.com",   # retained exec search
    "heidrick.com",
    "spencerstuart.com",
    "egonzehnder.com",
    "truesearch.com",
    "ondeckexec.com",
}


# Retained-search / exec recruiting firms — same HIGH confidence signal
# as ATS but listed separately for readability. Folded into
# RECRUITER_DOMAINS above.

# Domains that are AMBIGUOUS on their own — LinkedIn messages, for
# example, could be recruiter OR a friend. Require additional signals.
AMBIGUOUS_DOMAINS: set[str] = {
    "linkedin.com",   # messages-noreply@linkedin.com is often InMail
}


# ---------------------------------------------------------------------------
# Subject / snippet keyword patterns
# ---------------------------------------------------------------------------


# Phrases that strongly suggest recruiter outreach when in subject or
# snippet. Not sufficient alone (many emails say "opportunity") but
# contribute to a combined score.
_RECRUITER_PHRASES = [
    r"\bdirector\b", r"\bvp\b", r"\bhead of\b", r"\bchief\b", r"\bcto\b",
    r"\bcdo\b", r"\bsenior\b", r"\bexecutive\b",
    r"\breach(ing|ed)? out\b",
    r"\byour background\b", r"\byour experience\b", r"\byour profile\b",
    r"\bcame across\b",
    r"\binterested in (you|your)\b",
    r"\bopportunity\b", r"\bopening\b", r"\brole at\b",
    r"\binterview\b", r"\bphone screen\b", r"\bchat about\b",
    r"\bexplore (a |this )?\w+ role\b",
    r"\bcandidate\b",
    r"\bjoin (our|the)\b",
    r"\brecruiter\b", r"\brecruiting\b", r"\btalent\b",
    r"\bhiring\b",
]

_RECRUITER_RE = re.compile("|".join(_RECRUITER_PHRASES), flags=re.IGNORECASE)


# Subject keywords that indicate a recruiter pitch (subset of above,
# higher-signal on their own)
_STRONG_SUBJECT_PHRASES = [
    r"\bopportunity\b.*\b(director|vp|head|chief|senior)\b",
    r"\b(director|vp|head|chief)\b.*\b(role|opportunity|opening)\b",
    r"\breach(ing|ed)? out\b.*\b(about|regarding)\b",
]
_STRONG_SUBJECT_RE = re.compile("|".join(_STRONG_SUBJECT_PHRASES), flags=re.IGNORECASE)


# Negative keywords — phrases that suggest the email is NOT recruiting
# (transactional, newsletter, service). Reduce confidence.
# NOTE: "partnering with" was removed — recruiters routinely say
# "partnering with [hiring manager]" (ambiguous with sales "partnering
# with your team"). The recruiter-language signals dominate when both
# are present, and we don't want to drop genuine recruiter outreach.
_NON_RECRUITER_PHRASES = [
    r"\binvoice\b", r"\breceipt\b", r"\border (confirmation|shipped|status)\b",
    r"\bshipping\b", r"\bdemo (our|the) (platform|product)\b",
    r"\bsubscribe\b", r"\bunsubscribe\b",
    r"\bnewsletter\b", r"\bdigest\b",
]
_NON_RECRUITER_RE = re.compile("|".join(_NON_RECRUITER_PHRASES), flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_likely_recruiter(
    from_email: str | None,
    from_header: str | None,
    subject: str | None,
    snippet: str | None,
) -> tuple[bool, float, dict]:
    """Classify an inbound email as likely recruiter outreach.

    Returns (is_recruiter, confidence, signals) where:
      - is_recruiter: bool, True if confidence >= 0.5
      - confidence: float 0.0-1.0
      - signals: dict with 'reason' (human-readable) plus optional
        'matched_domain' / 'matched_keywords' for debugging

    Heuristics:
      - ATS / retained-search domain match → 0.95 confidence
      - Ambiguous domain (LinkedIn) + subject keywords → 0.7
      - Unknown domain + strong subject pattern + keyword-rich snippet → 0.65
      - Unknown domain + bare keyword → insufficient (< 0.5)
      - Negative keywords (invoice, demo, partnering) → subtract 0.3
    """
    from_email = (from_email or "").lower().strip()
    from_header = (from_header or "")
    subject = (subject or "")
    snippet = (snippet or "")

    if not from_email and not subject and not snippet:
        return False, 0.0, {"reason": "empty input"}

    confidence = 0.0
    reasons: list[str] = []
    matched_domain: str | None = None
    matched_keywords: list[str] = []

    # --- Domain match ---
    domain = from_email.partition("@")[2] if "@" in from_email else ""
    for rd in RECRUITER_DOMAINS:
        # Suffix match (endswith, with dot boundary to avoid false positives)
        if domain == rd or domain.endswith("." + rd):
            confidence = max(confidence, 0.95)
            matched_domain = rd
            reasons.append(f"ats_domain:{rd}")
            break

    is_ambiguous_domain = False
    if not matched_domain:
        for ad in AMBIGUOUS_DOMAINS:
            if domain == ad or domain.endswith("." + ad):
                is_ambiguous_domain = True
                reasons.append(f"ambiguous_domain:{ad}")
                break

    # --- Subject + snippet keyword scanning ---
    combined_text = f"{subject}\n{snippet}"
    keyword_hits = _RECRUITER_RE.findall(combined_text)
    if keyword_hits:
        # re.findall returns match groups (tuples if alternation); normalize
        matched_keywords = list({
            m if isinstance(m, str) else next((g for g in m if g), "")
            for m in keyword_hits
        })
        # Raw keyword count — more keywords = higher confidence
        unique_count = len([k for k in matched_keywords if k])

    strong_subject_match = bool(_STRONG_SUBJECT_RE.search(subject))

    # --- Scoring logic ---
    if matched_domain:
        # Already 0.95 from ATS domain
        pass
    elif is_ambiguous_domain:
        # LinkedIn-type: need keyword corroboration
        if strong_subject_match:
            confidence = max(confidence, 0.8)
            reasons.append("ambiguous_domain+strong_subject")
        elif keyword_hits:
            confidence = max(confidence, 0.6)
            reasons.append("ambiguous_domain+keywords")
        else:
            confidence = max(confidence, 0.2)
    else:
        # Unknown domain — require strong signals
        if strong_subject_match:
            confidence = max(confidence, 0.7)
            reasons.append("unknown_domain+strong_subject")
        elif keyword_hits and len(keyword_hits) >= 3:
            confidence = max(confidence, 0.6)
            reasons.append("unknown_domain+multi_keyword")
        elif keyword_hits:
            confidence = max(confidence, 0.35)
            reasons.append("unknown_domain+single_keyword")

    # --- Negative signal penalty ---
    if _NON_RECRUITER_RE.search(combined_text):
        confidence -= 0.3
        reasons.append("negative_keywords_penalty")

    confidence = max(0.0, min(1.0, confidence))

    signals = {"reason": " | ".join(reasons) if reasons else "no signals"}
    if matched_domain:
        signals["matched_domain"] = matched_domain
    if matched_keywords:
        signals["matched_keywords"] = matched_keywords

    return (confidence >= 0.5), confidence, signals
