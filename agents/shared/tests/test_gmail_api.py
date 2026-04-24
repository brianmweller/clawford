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

from agents.shared.gmail_api import build_raw_message, compose_reply_all_recipients


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


def test_raw_message_does_not_soft_wrap_long_paragraphs():
    """Python's default EmailMessage policy encodes bodies as
    quoted-printable with 76-column soft wraps (=\\n). Gmail's draft
    renderer treats those wraps as hard line breaks inside paragraphs,
    so a dehardwrapped single-line paragraph shows up in the compose
    window chopped at ~65-70 chars. Force CTE=8bit (or equivalent) so
    the body is transmitted as a single unwrapped line per paragraph.
    """
    long_paragraph = (
        "This is a long paragraph that the compose pipeline delivered "
        "as a single unwrapped line after dehardwrap. It should reach "
        "Gmail as one continuous line so Gmail wraps it to the user's "
        "window width, not at 76 columns with visible mid-sentence breaks."
    )
    raw = build_raw_message(
        to=["a@ex.com"],
        subject="Test",
        body=long_paragraph,
    )
    padded = raw + "=" * (-len(raw) % 4)
    mime_text = base64.urlsafe_b64decode(padded).decode("utf-8")
    # Header/body separator can be \r\n\r\n or \n\n depending on policy.
    sep = "\r\n\r\n" if "\r\n\r\n" in mime_text else "\n\n"
    _, body_text = mime_text.split(sep, 1)
    # The body, before any SMTP line-length enforcement, must contain
    # the paragraph verbatim as one line — no `=\n` soft breaks, no
    # mid-paragraph `\n` splits inserted by the email policy.
    assert "=\n" not in body_text and "=\r\n" not in body_text, (
        f"quoted-printable soft wraps must be absent; body was:\n{body_text!r}"
    )
    body_line = body_text.rstrip("\r\n")
    assert "\n" not in body_line, (
        f"single-paragraph body must not be wrapped into multiple lines; "
        f"got:\n{body_line!r}"
    )
    assert body_line.startswith("This is a long paragraph")
    assert body_line.endswith("visible mid-sentence breaks.")


def test_raw_message_preserves_paragraph_breaks():
    """Blank-line paragraph breaks (\\n\\n) must survive — they're how
    dehardwrap separates paragraphs downstream."""
    body = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    raw = build_raw_message(to=["a@ex.com"], subject="Test", body=body)
    padded = raw + "=" * (-len(raw) % 4)
    mime_text = base64.urlsafe_b64decode(padded).decode("utf-8")
    # Header/body separator can be \r\n\r\n or \n\n depending on policy.
    sep = "\r\n\r\n" if "\r\n\r\n" in mime_text else "\n\n"
    _, body_text = mime_text.split(sep, 1)
    assert "First paragraph." in body_text
    assert "Second paragraph." in body_text
    assert "Third paragraph." in body_text
    # Three paragraphs separated by blank lines → at least two blank-line
    # separators in the body.
    assert body_text.count("\r\n\r\n") + body_text.count("\n\n") >= 2


# ---------------------------------------------------------------------------
# compose_reply_all_recipients
# ---------------------------------------------------------------------------

OPERATOR = frozenset({"operator@example.com", "operator.alt@example.com"})


def test_reply_all_single_sender_leaves_cc_empty():
    inbound = {
        "from_email": "adler@example.com",
        "to": ["operator@example.com"],
        "cc": [],
    }
    to, cc = compose_reply_all_recipients(inbound, OPERATOR)
    assert to == ["adler@example.com"]
    assert cc == []


def test_reply_all_multi_recipient_ccs_others_back():
    inbound = {
        "from_email": "adler@example.com",
        "to": ["operator@example.com", "teammate@example.com"],
        "cc": ["observer@example.com"],
    }
    to, cc = compose_reply_all_recipients(inbound, OPERATOR)
    assert to == ["adler@example.com"]
    assert cc == ["teammate@example.com", "observer@example.com"]


def test_reply_all_excludes_all_operator_emails_from_cc():
    inbound = {
        "from_email": "adler@example.com",
        "to": ["operator@example.com", "teammate@example.com"],
        "cc": ["operator.alt@example.com"],
    }
    to, cc = compose_reply_all_recipients(inbound, OPERATOR)
    assert to == ["adler@example.com"]
    assert cc == ["teammate@example.com"]


def test_reply_all_excludes_sender_from_cc_if_cc_listed():
    # Defensive: if the inbound message Cc'd its own sender (weird but
    # possible), the reply's Cc must not duplicate the To.
    inbound = {
        "from_email": "adler@example.com",
        "to": ["operator@example.com"],
        "cc": ["adler@example.com", "observer@example.com"],
    }
    to, cc = compose_reply_all_recipients(inbound, OPERATOR)
    assert to == ["adler@example.com"]
    assert cc == ["observer@example.com"]


def test_reply_all_case_insensitive_dedup():
    inbound = {
        "from_email": "Adler@Example.com",
        "to": ["OPERATOR@example.com", "Teammate@Example.com"],
        "cc": ["teammate@example.com", "the operator.Alt@example.com"],
    }
    to, cc = compose_reply_all_recipients(inbound, OPERATOR)
    # To keeps inbound casing for the sender.
    assert to == ["Adler@Example.com"]
    # Cc deduped by lowercase; first-seen casing preserved.
    assert cc == ["Teammate@Example.com"]


def test_reply_all_preserves_to_then_cc_ordering():
    inbound = {
        "from_email": "adler@example.com",
        "to": ["a@example.com", "b@example.com", "operator@example.com"],
        "cc": ["c@example.com", "d@example.com"],
    }
    to, cc = compose_reply_all_recipients(inbound, OPERATOR)
    assert to == ["adler@example.com"]
    assert cc == ["a@example.com", "b@example.com", "c@example.com", "d@example.com"]


def test_reply_all_missing_to_cc_keys_defaults_empty():
    inbound = {"from_email": "adler@example.com"}
    to, cc = compose_reply_all_recipients(inbound, OPERATOR)
    assert to == ["adler@example.com"]
    assert cc == []
