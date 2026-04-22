"""agents/shared/recruiter_domains.py — fleet-shared set of ATS /
retained-search / exec recruiting platform domains.

Single source of truth for any agent that needs to recognize a
recruiter-platform email address:

  - Huckle's inbox triage (connector's recruiter_detector_lib imports
    this to decide between skipped_unknown_sender and
    queued_cold_recruiter for senders not in the operator's people map)
  - Huckle's sent-mail voice-profile build (voice-profile-build.py
    --recruiter-mode queries Gmail for sent messages to these domains)
  - Murphy's meeting-prep classifier (meeting_prep_professional_lib
    uses this to classify a calendar event as a recruiter screen when
    an attendee's domain hits the set)
  - Huckle's search-status miner (search-status-build.py mines
    interview-stage signals from threads whose sender matches this set)

Matching is always done by domain-suffix, so subdomain variants like
`hire.lever.co` and `mail.greenhouse-mail.io` resolve correctly.
"""
from __future__ import annotations


# ATS / recruiting platform domains.
RECRUITER_DOMAINS: frozenset[str] = frozenset({
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
    "notion.so",
    "mail.notion.so",
    "mail.hire.com",
    "rivierapartners.com",
    "russellreynolds.com",
    "heidrick.com",
    "spencerstuart.com",
    "egonzehnder.com",
    "truesearch.com",
    "ondeckexec.com",
})


# Domains that are ambiguous on their own — LinkedIn InMail could be
# recruiter OR a friend. Require additional signals (subject keywords,
# exec-outreach phrasing) before classifying as recruiter.
AMBIGUOUS_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com",
})


# ATS subdomain prefixes — big companies run in-house scheduling on
# subdomains like `interview.adobe.com`, `schedule.amazon.com`,
# `recruiting.google.com`, `talent.apple.com`. The organizer email on
# those invites is typically `schedule@<prefix>.<company>.com` or
# `no-reply@<prefix>.<company>.com`. Enumerating every big company's
# ATS domain is a losing battle; matching the subdomain prefix catches
# the pattern generically.
_ATS_SUBDOMAIN_PREFIXES: frozenset[str] = frozenset({
    "interview",
    "interviews",
    "schedule",
    "scheduling",
    "recruiting",
    "recruit",
    "talent",
    "careers",
    "hire",
    "hiring",
})


def is_recruiter_domain(email_or_domain: str) -> bool:
    """True if the argument's domain suffix matches RECRUITER_DOMAINS
    or its leftmost subdomain is a known ATS prefix
    (`interview.adobe.com`, `schedule.amazon.com`, etc.).

    Accepts either a bare domain (`lever.co`) or a full email
    (`no-reply@lever.co`). Case-insensitive. Empty / None → False.
    """
    if not email_or_domain:
        return False
    text = email_or_domain.lower().strip()
    domain = text.partition("@")[2] if "@" in text else text
    if not domain:
        return False
    for rd in RECRUITER_DOMAINS:
        if domain == rd or domain.endswith("." + rd):
            return True
    # Subdomain-prefix pattern: only the leftmost label is considered,
    # and only if the domain has at least 3 labels (prefix.company.tld).
    # This avoids matching a bare `careers.com` (2 labels, likely a
    # legitimate recruiting site but also a potential false-positive
    # seed). 3+ labels implies `prefix.company.tld` form.
    parts = domain.split(".")
    if len(parts) >= 3 and parts[0] in _ATS_SUBDOMAIN_PREFIXES:
        return True
    return False


def extract_company_from_ats_domain(email_or_domain: str) -> str | None:
    """Return the capitalized company stem from an in-house ATS
    subdomain, or None when no company can be inferred.

    Examples:
      'schedule@interview.adobe.com' → 'Adobe'
      'no-reply@recruiting.amazon.com' → 'Amazon'
      'talent.google.com' → 'Google'
      'no-reply@greenhouse-mail.io' → None   (3rd-party ATS platform)
      'jane@lever.co' → None                 (3rd-party ATS platform)
      'boss@example.com' → None              (not recruiter-adjacent)

    Used by the Workflowy title-derivation path so recruiter invites
    with no attendees still get a company hashtag. Deliberately
    conservative — only 'prefix.company.tld' forms with a recognized
    ATS prefix resolve, since anything else risks mis-attributing a
    friend's employer (`alice@google.com`) as the interview target.
    """
    if not email_or_domain:
        return None
    text = email_or_domain.lower().strip()
    domain = text.partition("@")[2] if "@" in text else text
    if not domain:
        return None
    # Third-party ATS platforms (greenhouse, lever, etc.) are just
    # message carriers — never treat their domain as the company.
    for rd in RECRUITER_DOMAINS:
        if domain == rd or domain.endswith("." + rd):
            return None
    parts = domain.split(".")
    if len(parts) < 3 or parts[0] not in _ATS_SUBDOMAIN_PREFIXES:
        return None
    # prefix.company.tld (or prefix.company.co.uk) — take the label
    # right after the prefix as the company stem.
    stem = parts[1]
    if not stem or not stem.isalnum():
        return None
    return stem[:1].upper() + stem[1:]
