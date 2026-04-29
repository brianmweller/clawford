"""Pure helpers for inbox-triage.py.

classify_thread_for_triage: given a thread's latest message (metadata
form) plus the email→slug map and the operator's own addresses, classify as
queued-for-draft OR one of several skip reasons.

No Gmail API calls here; the script layer fetches threads and feeds
them in. Tests use hand-built thread dicts — no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from inbox_triage_lib import (  # type: ignore
    build_queue_from_results,
    classify_thread_for_triage,
    extract_email_from_header,
    latest_message,
    upsert_thread_in_queue,
)


BRIAN_ADDRESSES = {
    "sam.smith@example.com",
    "sam.smith+backup@example.com",
    "sam.smith+work@example.com",
}


def _headers(**kwargs):
    """Gmail API returns headers as [{name, value}, ...]; helper builds that."""
    return [{"name": k, "value": v} for k, v in kwargs.items()]


def _message(from_, date, subject="Test subject", body_snippet="", message_id=None):
    payload = {"headers": _headers(**{"From": from_, "Date": date, "Subject": subject})}
    if message_id:
        payload["headers"].append({"name": "Message-ID", "value": message_id})
    return {"payload": payload, "snippet": body_snippet}


def _thread(thread_id, messages):
    return {"id": thread_id, "messages": messages}


# --- extract_email_from_header ---

def test_extract_plain_email():
    assert extract_email_from_header("cherry@anthropic.com") == "cherry@anthropic.com"


def test_extract_named_email():
    assert extract_email_from_header("Josh Cherry <cherry@anthropic.com>") == "cherry@anthropic.com"


def test_extract_preserves_plus_and_dots():
    assert (
        extract_email_from_header("Sam Smith <sam.smith+foo@gmail.com>")
        == "sam.smith+foo@gmail.com"
    )


def test_extract_returns_empty_for_junk():
    assert extract_email_from_header("") == ""
    assert extract_email_from_header("no angle brackets or at") == ""


# --- latest_message ---

def test_latest_message_picks_last_in_list():
    # Gmail returns messages in chronological order; "latest" is last
    thread = _thread("t1", [
        _message("a@ex.com", "Mon, 14 Apr 2026 10:00:00 -0700"),
        _message("b@ex.com", "Tue, 15 Apr 2026 10:00:00 -0700"),
    ])
    latest = latest_message(thread)
    assert latest["payload"]["headers"][0]["value"] == "b@ex.com"


def test_latest_message_empty_thread():
    assert latest_message(_thread("t1", [])) is None


# --- classify_thread_for_triage ---

def test_thread_where_brian_replied_last_is_skipped():
    thread = _thread("t1", [
        _message("josh@anthropic.com", "Mon, 14 Apr 2026 10:00:00 -0700"),
        _message("sam.smith@example.com", "Mon, 14 Apr 2026 11:00:00 -0700"),
    ])
    result = classify_thread_for_triage(
        thread,
        operator_emails=BRIAN_ADDRESSES,
        email_to_slug={"josh@anthropic.com": "josh-cherry"},
    )
    assert result["status"] == "skipped_brian_last"


def test_thread_with_known_sender_last_is_queued():
    thread = _thread("t1", [
        _message("sam.smith@example.com", "Mon, 14 Apr 2026 10:00:00 -0700"),
        _message("Josh Cherry <cherry@anthropic.com>", "Tue, 15 Apr 2026 10:00:00 -0700",
                 subject="Re: Hi", body_snippet="Following up..."),
    ])
    result = classify_thread_for_triage(
        thread,
        operator_emails=BRIAN_ADDRESSES,
        email_to_slug={"cherry@anthropic.com": "josh-cherry"},
    )
    assert result["status"] == "queued"
    assert result["slug"] == "josh-cherry"
    assert result["from_email"] == "cherry@anthropic.com"
    assert result["thread_id"] == "t1"
    assert result["subject"] == "Re: Hi"


def test_thread_with_service_sender_last_is_skipped():
    thread = _thread("t1", [
        _message("notifications@stripe.com", "Tue, 15 Apr 2026 10:00:00 -0700"),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "skipped_service"


def test_ambiguous_domain_without_recruiter_signal_falls_to_unknown():
    """LinkedIn notification-style sender with no recruiter phrasing
    is no longer bucketed as skipped_service — AMBIGUOUS_DOMAINS are
    exempt from the service-account short-circuit. With no recruiter
    signals it lands in skipped_unknown_sender (still not drafted)."""
    thread = _thread("t1", [
        _message("noreply@linkedin.com", "Tue, 15 Apr 2026 10:00:00 -0700"),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "skipped_unknown_sender"


def test_thread_with_unknown_sender_last_is_skipped():
    thread = _thread("t1", [
        _message("strange.new.contact@nowhere.com", "Tue, 15 Apr 2026 10:00:00 -0700"),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "skipped_unknown_sender"


def test_thread_with_unknown_recruiter_sender_is_queued_cold():
    """Unknown sender whose domain is an ATS platform (greenhouse-mail.io)
    routes to queued_cold_recruiter, not skipped_unknown_sender."""
    thread = _thread("t1", [
        _message("no-reply@greenhouse-mail.io", "Tue, 15 Apr 2026 10:00:00 -0700",
                 subject="Opportunity — Director of Data Science at Stripe",
                 body_snippet="Hi the operator, reaching out about a senior role..."),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "queued_cold_recruiter"
    assert result["recruiter_signal_confidence"] >= 0.9
    assert "greenhouse-mail.io" in result.get("recruiter_matched_domain", "")


def test_thread_with_unknown_non_recruiter_unknown_stays_skipped():
    """Unknown sender without recruiter signals still skips (no drafting)."""
    thread = _thread("t1", [
        _message("mom@nowhere.com", "Tue, 15 Apr 2026 10:00:00 -0700",
                 subject="dinner sunday?",
                 body_snippet="want to come over this weekend?"),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "skipped_unknown_sender"


def test_rejected_recruiter_short_circuits_before_detector():
    """Once the operator has tapped 🚫 Not a fit on a prior cold-recruiter FYI,
    future inbounds from the same sender return skipped_rejected_recruiter
    and never reach the detector."""
    thread = _thread("t1", [
        _message("no-reply@greenhouse-mail.io", "Tue, 15 Apr 2026 10:00:00 -0700",
                 subject="Opportunity — Director of Data Science at Stripe",
                 body_snippet="Hi the operator, reaching out..."),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
        rejected_recruiters={"no-reply@greenhouse-mail.io"},
    )
    assert result["status"] == "skipped_rejected_recruiter"


def test_rejected_recruiters_default_none_unchanged_behavior():
    """Omitting rejected_recruiters must not change existing classification."""
    thread = _thread("t1", [
        _message("no-reply@greenhouse-mail.io", "Tue, 15 Apr 2026 10:00:00 -0700",
                 subject="Opportunity — Director of Data Science",
                 body_snippet="Reaching out about a role..."),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "queued_cold_recruiter"


def test_linkedin_inmail_with_exec_role_subject_queued_cold():
    """Ambiguous LinkedIn messages-noreply + exec-outreach subject → cold recruiter."""
    thread = _thread("t1", [
        _message("messages-noreply@linkedin.com", "Tue, 15 Apr 2026 10:00:00 -0700",
                 subject="Senior opportunity — Director of Data Science, Marketplace",
                 body_snippet="Reaching out about an executive role..."),
    ])
    # Note: LinkedIn messages-noreply matches is_likely_service_account's
    # service-local prefix list, so this path is tested for the case
    # where the service detector isn't hitting it. Check the service
    # detector first behavior explicitly elsewhere.
    # For now, verify the recruiter detector would catch it if service
    # didn't: direct check.
    from recruiter_detector_lib import is_likely_recruiter
    ok, conf, _ = is_likely_recruiter(
        from_email="messages-noreply@linkedin.com",
        from_header='"Jane Smith" <messages-noreply@linkedin.com>',
        subject="Senior opportunity — Director of Data Science, Marketplace",
        snippet="Reaching out about an executive role...",
    )
    assert ok
    assert conf >= 0.5


def test_linkedin_inmail_with_exec_role_subject_routes_cold_not_service():
    """LinkedIn InMail from a recruiter (inmail-hit-reply@linkedin.com with
    'via LinkedIn' in display name) must NOT short-circuit as
    skipped_service. linkedin.com is in AMBIGUOUS_DOMAINS — the triage
    should defer to the recruiter detector, which will upgrade this to
    queued_cold_recruiter on the strength of subject signals."""
    thread = _thread("t1", [
        _message(
            "Iman Recruiter via LinkedIn <inmail-hit-reply@linkedin.com>",
            "Wed, 22 Apr 2026 13:52:00 -0700",
            subject="Connecting re: Senior Director of Data Science & Analytics for Adobe Firefly",
            body_snippet="Hi the operator, I'm part of the Adobe recruiting team reaching out about an executive role...",
        ),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "queued_cold_recruiter"


def test_thread_with_empty_messages_is_skipped():
    result = classify_thread_for_triage(
        _thread("t1", []),
        operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "skipped_empty"


def test_classify_is_case_insensitive_on_sender_email():
    thread = _thread("t1", [
        _message("CHERRY@ANTHROPIC.COM", "Tue, 15 Apr 2026 10:00:00 -0700",
                 subject="Re: Hi"),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES,
        email_to_slug={"cherry@anthropic.com": "josh-cherry"},
    )
    assert result["status"] == "queued"
    assert result["slug"] == "josh-cherry"


def test_classify_captures_message_id_for_threading():
    thread = _thread("t1", [
        _message("cherry@anthropic.com", "Tue, 15 Apr 2026 10:00:00 -0700",
                 subject="Re: Hi", message_id="<abc123@mail.gmail.com>"),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES,
        email_to_slug={"cherry@anthropic.com": "josh-cherry"},
    )
    assert result["status"] == "queued"
    assert result["in_reply_to_message_id"] == "<abc123@mail.gmail.com>"


# --- upsert_thread_in_queue ---


def _classified_queued(thread_id="t1", slug="josh-cherry"):
    return {
        "thread_id": thread_id, "slug": slug,
        "from_email": f"{slug}@example.com",
        "from_header": f"{slug} <{slug}@example.com>",
        "subject": "Hi", "date": "Tue, 15 Apr 2026 10:00:00 -0700",
        "snippet": "hello", "in_reply_to_message_id": f"<{thread_id}@m>",
        "status": "queued",
    }


def test_upsert_appends_new_queued_thread_to_empty_queue():
    result = upsert_thread_in_queue(None, _classified_queued("t1"))
    assert len(result["queued"]) == 1
    assert result["queued"][0]["thread_id"] == "t1"
    assert result["queued"][0]["slug"] == "josh-cherry"
    # status is a transient classifier diagnostic, not persisted
    assert "status" not in result["queued"][0]


def test_upsert_replaces_existing_entry_for_same_thread_id():
    queue = {"queued": [_classified_queued("t1", slug="old-slug")]}
    # scrub the "status" field that upsert strips on insertion
    queue["queued"][0].pop("status", None)
    new = _classified_queued("t1", slug="new-slug")
    result = upsert_thread_in_queue(queue, new)
    assert len(result["queued"]) == 1
    assert result["queued"][0]["slug"] == "new-slug"


def test_upsert_preserves_other_threads():
    queue = {"queued": [
        {"thread_id": "t2", "slug": "other"},
        {"thread_id": "t3", "slug": "third"},
    ]}
    new = _classified_queued("t1")
    result = upsert_thread_in_queue(queue, new)
    tids = [e["thread_id"] for e in result["queued"]]
    assert set(tids) == {"t1", "t2", "t3"}


def test_upsert_drops_entry_when_classified_is_skipped():
    """A thread that reclassifies as skipped_brian_last (e.g. the operator
    replied between polling scan and push event) must be removed from
    the queue, not re-added."""
    queue = {"queued": [
        {"thread_id": "t1", "slug": "josh-cherry"},
        {"thread_id": "t2", "slug": "other"},
    ]}
    skipped = {
        "thread_id": "t1",
        "from_email": "operator@example.com",
        "status": "skipped_brian_last",
    }
    result = upsert_thread_in_queue(queue, skipped)
    tids = [e["thread_id"] for e in result["queued"]]
    assert tids == ["t2"]


def test_upsert_skipped_on_empty_queue_is_noop():
    skipped = {"thread_id": "t1", "status": "skipped_service"}
    result = upsert_thread_in_queue(None, skipped)
    assert result["queued"] == []


def test_upsert_preserves_non_queued_fields_in_queue_dict():
    queue = {"queued": [], "generated_at": "2026-04-20T00:00:00Z"}
    result = upsert_thread_in_queue(queue, _classified_queued("t1"))
    assert result["generated_at"] == "2026-04-20T00:00:00Z"


def test_upsert_appends_cold_recruiter_with_status_field():
    """Cold-recruiter entries must persist with status=queued_cold_recruiter
    and recruiter signal fields, so auto-compose can dispatch them in
    cold-inbound mode."""
    cold = {
        "thread_id": "t1",
        "from_email": "no-reply@greenhouse-mail.io",
        "from_header": "Recruiter via Greenhouse <no-reply@greenhouse-mail.io>",
        "subject": "Senior Director role at Stripe",
        "date": "Tue, 15 Apr 2026 10:00:00 -0700",
        "snippet": "Reaching out about a senior role...",
        "in_reply_to_message_id": "<x@m>",
        "status": "queued_cold_recruiter",
        "recruiter_signal_confidence": 0.95,
        "recruiter_signal_reason": "ats_domain",
        "recruiter_matched_domain": "greenhouse-mail.io",
    }
    result = upsert_thread_in_queue(None, cold)
    assert len(result["queued"]) == 1
    entry = result["queued"][0]
    assert entry["status"] == "queued_cold_recruiter"
    assert entry["recruiter_signal_confidence"] == 0.95
    assert entry["recruiter_matched_domain"] == "greenhouse-mail.io"
    assert "slug" not in entry  # cold recruiters have no slug yet


# --- build_queue_from_results ---


def test_build_queue_persists_both_queued_and_cold_recruiter():
    """Full-scan persistence must keep regular queued items AND cold
    recruiters. Skipped statuses must be absent."""
    results = [
        _classified_queued("t1", slug="known-person"),
        {
            "thread_id": "t2",
            "from_email": "no-reply@greenhouse-mail.io",
            "from_header": "Recruiter via Greenhouse <no-reply@greenhouse-mail.io>",
            "subject": "Director role at Stripe",
            "date": "Tue, 15 Apr 2026 10:00:00 -0700",
            "snippet": "Reaching out...",
            "in_reply_to_message_id": "<m@x>",
            "status": "queued_cold_recruiter",
            "recruiter_signal_confidence": 0.9,
            "recruiter_signal_reason": "ats_domain",
            "recruiter_matched_domain": "greenhouse-mail.io",
        },
        {"thread_id": "t3", "status": "skipped_service"},
        {"thread_id": "t4", "status": "skipped_unknown_sender"},
    ]
    queue = build_queue_from_results(results)
    tids = [e["thread_id"] for e in queue["queued"]]
    assert "t1" in tids
    assert "t2" in tids
    assert "t3" not in tids
    assert "t4" not in tids


def test_build_queue_from_empty_results_returns_empty_queued():
    queue = build_queue_from_results([])
    assert queue == {"queued": []}


def test_build_queue_idempotent_on_duplicate_thread_ids():
    """A push-listener can re-classify a thread that the full-scan also
    saw; the second classification should replace the first."""
    results = [
        _classified_queued("t1", slug="old-slug"),
        _classified_queued("t1", slug="new-slug"),
    ]
    queue = build_queue_from_results(results)
    assert len(queue["queued"]) == 1
    assert queue["queued"][0]["slug"] == "new-slug"
