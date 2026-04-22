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


def is_recruiter_domain(email_or_domain: str) -> bool:
    """True if the argument's domain suffix matches RECRUITER_DOMAINS.

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
    return False
