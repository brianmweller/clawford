"""Thread → compose-inputs helpers.

thread_to_compose_inputs() takes a Gmail threads.get(format=full) response
and returns (inbound_dict, history_list) matching the fixture JSON shape
draft-compose.py consumes. Lets us skip the fixture-file step and fetch
from Gmail directly.

extract_plain_body() walks the Gmail message payload (simple or multipart)
and returns the plain-text body.
"""
from __future__ import annotations

import base64

from agents.shared.gmail_api import (
    extract_plain_body,
    thread_to_compose_inputs,
)


OPERATOR = {"sam.smith@example.com"}


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _headers(**kw):
    return [{"name": k, "value": v} for k, v in kw.items()]


def _msg(id_, from_, subject, date, body="", is_multipart=False):
    payload = {"headers": _headers(**{"From": from_, "Subject": subject, "Date": date})}
    if is_multipart:
        payload["mimeType"] = "multipart/alternative"
        payload["parts"] = [
            {"mimeType": "text/plain", "body": {"data": _b64(body)}},
            {"mimeType": "text/html", "body": {"data": _b64(f"<p>{body}</p>")}},
        ]
    else:
        payload["mimeType"] = "text/plain"
        payload["body"] = {"data": _b64(body)}
    return {"id": id_, "payload": payload}


# --- extract_plain_body ---

def test_extract_plain_body_simple():
    m = _msg("m1", "a@x.com", "Hi", "Mon, 14 Apr 2026 10:00:00 -0700", body="Hello there.")
    assert extract_plain_body(m) == "Hello there."


def test_extract_plain_body_multipart_prefers_text_plain():
    m = _msg("m1", "a@x.com", "Hi", "Mon, 14 Apr 2026 10:00:00 -0700",
            body="Plain content", is_multipart=True)
    body = extract_plain_body(m)
    assert body == "Plain content"
    # Must NOT have the HTML-wrapped version
    assert "<p>" not in body


def test_extract_plain_body_empty_when_no_data():
    m = {"payload": {"headers": [], "body": {}}}
    assert extract_plain_body(m) == ""


def test_extract_plain_body_preserves_utf8():
    m = _msg("m1", "a@x.com", "Hi", "Mon, 14 Apr 2026 10:00:00 -0700",
            body="Café — naïve piña colada")
    body = extract_plain_body(m)
    assert "Café" in body
    assert "—" in body


# --- thread_to_compose_inputs ---

def test_thread_extracts_latest_inbound_as_inbound():
    thread = {"id": "t1", "messages": [
        _msg("m1", "sam.smith@example.com", "Original",
             "Mon, 14 Apr 2026 10:00:00 -0700", body="the operator's first msg"),
        _msg("m2", "Josh Cherry <cherry@anthropic.com>", "Re: Original",
             "Tue, 15 Apr 2026 10:00:00 -0700", body="Josh reply text"),
    ]}
    inbound, history = thread_to_compose_inputs(thread, OPERATOR)
    assert inbound["from_email"] == "cherry@anthropic.com"
    assert inbound["from_name"] == "Josh Cherry"
    assert inbound["subject"] == "Re: Original"
    assert "Josh reply text" in inbound["body"]


def test_thread_history_covers_messages_before_latest_inbound():
    thread = {"id": "t1", "messages": [
        _msg("m1", "sam.smith@example.com", "Original",
             "Mon, 14 Apr 2026 10:00:00 -0700", body="the operator msg 1"),
        _msg("m2", "Josh Cherry <cherry@anthropic.com>", "Re: Original",
             "Mon, 14 Apr 2026 11:00:00 -0700", body="Josh msg 1"),
        _msg("m3", "sam.smith@example.com", "Re: Original",
             "Mon, 14 Apr 2026 12:00:00 -0700", body="the operator msg 2"),
        _msg("m4", "Josh Cherry <cherry@anthropic.com>", "Re: Original",
             "Tue, 15 Apr 2026 10:00:00 -0700", body="Josh msg 2 latest"),
    ]}
    inbound, history = thread_to_compose_inputs(thread, OPERATOR)
    assert inbound["body"].strip() == "Josh msg 2 latest"
    # History should be the three earlier messages
    assert len(history) == 3
    # First entry is the operator's first message
    assert history[0]["from"] == "the operator"
    assert "the operator msg 1" in history[0]["body"]
    # Josh's intermediate message is in history
    assert any("Josh msg 1" in h["body"] for h in history)
    # the operator's second message is in history
    assert any("the operator msg 2" in h["body"] for h in history)


def test_thread_with_only_brian_messages_raises():
    thread = {"id": "t1", "messages": [
        _msg("m1", "sam.smith@example.com", "Note", "Mon, 14 Apr 2026 10:00:00 -0700"),
    ]}
    try:
        thread_to_compose_inputs(thread, OPERATOR)
    except ValueError:
        return
    raise AssertionError("expected ValueError for thread with no inbound")


def test_thread_empty_raises():
    try:
        thread_to_compose_inputs({"id": "t1", "messages": []}, OPERATOR)
    except ValueError:
        return
    raise AssertionError("expected ValueError for empty thread")


def test_thread_brian_shown_as_brian_in_history():
    thread = {"id": "t1", "messages": [
        _msg("m1", "Sam Smith <sam.smith@example.com>", "Hi",
             "Mon, 14 Apr 2026 10:00:00 -0700", body="B1"),
        _msg("m2", "Josh Cherry <cherry@anthropic.com>", "Re: Hi",
             "Tue, 15 Apr 2026 10:00:00 -0700", body="J1"),
    ]}
    _inbound, history = thread_to_compose_inputs(thread, OPERATOR)
    assert history[0]["from"] == "the operator"


def test_thread_non_brian_shown_with_display_name_in_history():
    thread = {"id": "t1", "messages": [
        _msg("m1", "Sam Sidekick <sam@x.com>", "Hi",
             "Mon, 14 Apr 2026 10:00:00 -0700", body="S1"),
        _msg("m2", "Josh Cherry <cherry@anthropic.com>", "Re: Hi",
             "Tue, 15 Apr 2026 10:00:00 -0700", body="J1"),
    ]}
    _inbound, history = thread_to_compose_inputs(thread, OPERATOR)
    # When non-the operator sender has a display name, use it
    assert history[0]["from"] == "Sam Sidekick"
