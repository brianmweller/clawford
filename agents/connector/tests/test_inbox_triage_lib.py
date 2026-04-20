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
    classify_thread_for_triage,
    extract_email_from_header,
    latest_message,
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
        _message("noreply@linkedin.com", "Tue, 15 Apr 2026 10:00:00 -0700"),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "skipped_service"


def test_thread_with_unknown_sender_last_is_skipped():
    thread = _thread("t1", [
        _message("strange.new.contact@nowhere.com", "Tue, 15 Apr 2026 10:00:00 -0700"),
    ])
    result = classify_thread_for_triage(
        thread, operator_emails=BRIAN_ADDRESSES, email_to_slug={},
    )
    assert result["status"] == "skipped_unknown_sender"


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
