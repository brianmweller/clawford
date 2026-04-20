"""Pure helpers for building RFC822 messages for the Gmail draft API.

build_raw_message takes To/Subject/Body/CC and optional In-Reply-To, and
returns the base64url-encoded raw message the Gmail API expects. The
threaded-draft wrapper (create_threaded_draft) uses this + threadId to
create a draft that lands IN the existing thread, not as a standalone.
"""
from __future__ import annotations

import base64
import email
from email.message import EmailMessage

from agents.shared.gmail_api import build_raw_message


def _decode(raw: str) -> EmailMessage:
    padded = raw + "=" * (-len(raw) % 4)
    msg_bytes = base64.urlsafe_b64decode(padded)
    return email.message_from_bytes(msg_bytes)


def test_raw_message_carries_to_subject_body():
    raw = build_raw_message(
        to=["adler.xie@gmail.com"],
        subject="Re: Greetings",
        body="Hi Adler -- tuesday works.\n\nBrian",
    )
    msg = _decode(raw)
    assert msg["To"] == "adler.xie@gmail.com"
    assert msg["Subject"] == "Re: Greetings"
    assert "tuesday works" in msg.get_payload()


def test_raw_message_supports_multiple_to():
    raw = build_raw_message(
        to=["a@ex.com", "b@ex.com"],
        subject="Hi",
        body="yo",
    )
    msg = _decode(raw)
    assert "a@ex.com" in msg["To"]
    assert "b@ex.com" in msg["To"]


def test_raw_message_supports_cc():
    raw = build_raw_message(
        to=["a@ex.com"],
        subject="Hi",
        body="yo",
        cc=["c@ex.com"],
    )
    msg = _decode(raw)
    assert msg["Cc"] == "c@ex.com"


def test_raw_message_sets_in_reply_to_when_provided():
    raw = build_raw_message(
        to=["a@ex.com"],
        subject="Re: prior",
        body="reply body",
        in_reply_to_message_id="<CAN1abc@mail.gmail.com>",
    )
    msg = _decode(raw)
    assert msg["In-Reply-To"] == "<CAN1abc@mail.gmail.com>"
    assert msg["References"] == "<CAN1abc@mail.gmail.com>"


def test_raw_message_omits_threading_headers_when_not_provided():
    raw = build_raw_message(to=["a@ex.com"], subject="New", body="body")
    msg = _decode(raw)
    assert "In-Reply-To" not in msg
    assert "References" not in msg


def test_raw_message_is_base64_urlsafe():
    raw = build_raw_message(to=["a@ex.com"], subject="Hi", body="body")
    # base64url alphabet: A-Z a-z 0-9 - _ =
    for c in raw:
        assert c.isalnum() or c in "-_=", f"non-urlsafe char: {c!r}"


def test_raw_message_preserves_unicode_body():
    # Em-dashes and other non-ASCII must survive the encoding round-trip
    raw = build_raw_message(
        to=["a@ex.com"],
        subject="Test",
        body="Hi — here's a message with a naïve em-dash.",
    )
    msg = _decode(raw)
    payload = msg.get_payload(decode=True).decode("utf-8")
    assert "—" in payload
    assert "naïve" in payload
