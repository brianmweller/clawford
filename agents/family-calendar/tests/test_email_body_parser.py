"""Tests for activity-email-check.get_email_body().

Diagnosed against 8 real seen-cache messages where 5/8 returned
empty bodies:

- top-level text/html with no parts     → 3 Tutu messages
- multipart/mixed → alternative → p+h   → 1 Stratford SD campus message
- text/plain stub that wins over HTML   → 1 Smore weekly update

The new parser walks parts recursively, prefers useful text/plain,
and falls through to HTML-stripped content when plain is missing or
too short to be useful.

Run: cd agents/family-calendar && python3 -m pytest tests/test_email_body_parser.py -v
"""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script():
    path = SCRIPTS_DIR / "activity-email-check.py"
    spec = importlib.util.spec_from_file_location("famcal_activity_email_check", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def aec():
    return _load_script()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _leaf(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": _b64(text)}, "parts": []}


def _container(mime: str, children: list[dict]) -> dict:
    return {"mimeType": mime, "body": {}, "parts": children}


def _msg(payload: dict) -> dict:
    return {"payload": payload}


def test_top_level_text_plain(aec):
    body = aec.get_email_body(_msg(_leaf("text/plain", "hello world")))
    assert body == "hello world"


def test_top_level_text_html_with_no_parts(aec):
    """Tutu School recital emails — payload is text/html at the top,
    no parts array. The old parser returned ""."""
    html = "<html><body><p>Your recital is on April 20.</p></body></html>"
    body = aec.get_email_body(_msg(_leaf("text/html", html)))
    assert "Your recital is on April 20." in body
    assert "<p>" not in body


def test_multipart_alternative_prefers_plain(aec):
    payload = _container("multipart/alternative", [
        _leaf("text/plain", "Dear Stratford parents, survey is open. " * 10),
        _leaf("text/html", "<p>Dear Stratford parents, survey is open.</p>"),
    ])
    body = aec.get_email_body(_msg(payload))
    assert "Dear Stratford parents" in body
    assert "<p>" not in body  # confirms plain was chosen, not html


def test_multipart_alternative_falls_through_when_plain_is_stub(aec):
    """Smore-style emails ship a tiny text/plain 'enable HTML' stub
    alongside the real 112 KB HTML part. Parser must fall through."""
    stub = "\r\n"  # the real-world case was 4 bytes, strips to 2
    html = "<html><body>" + "Campus weekly update. " * 200 + "</body></html>"
    payload = _container("multipart/alternative", [
        _leaf("text/plain", stub),
        _leaf("text/html", html),
    ])
    body = aec.get_email_body(_msg(payload))
    assert "Campus weekly update" in body
    assert "<html>" not in body


def test_nested_multipart_mixed_with_inner_alternative(aec):
    """Stratford 'Mr. Seghir Day' — multipart/mixed wraps
    multipart/alternative + image/png. Old parser walked only
    one level and missed the plain+html inside."""
    inner = _container("multipart/alternative", [
        _leaf("text/plain", "Mr. Seghir Day is Friday April 17. " * 10),
        _leaf("text/html", "<p>Mr. Seghir Day is Friday April 17.</p>"),
    ])
    image = {"mimeType": "image/png", "body": {"attachmentId": "abc"}, "parts": []}
    payload = _container("multipart/mixed", [inner, image])
    body = aec.get_email_body(_msg(payload))
    assert "Mr. Seghir Day" in body
    assert "<p>" not in body


def test_empty_payload_returns_empty_string(aec):
    body = aec.get_email_body(_msg({}))
    assert body == ""


def test_html_only_nested(aec):
    """text/html buried inside nested containers, no text/plain anywhere."""
    inner = _container("multipart/related", [
        _leaf("text/html", "<div>Recital sign-up is open.</div>"),
    ])
    payload = _container("multipart/mixed", [inner])
    body = aec.get_email_body(_msg(payload))
    assert "Recital sign-up is open." in body
    assert "<div>" not in body
