"""Tests for scripts/fleet-oauth-daemon.py.

Pure helpers (HMAC signing, state-file lifecycle, OAuth-URL building,
token fan-out) are exercised directly. The HTTP listener is exercised
via in-process request handlers with subprocess/network calls stubbed
so the suite never opens a port or talks to Google.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "fleet-oauth-daemon.py"


def _import_module():
    spec = importlib.util.spec_from_file_location("fleet_oauth_daemon", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_oauth_daemon"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── HMAC link tokens (Telegram → /oauth/start gate) ─────────────────


def test_make_and_verify_signed_token_roundtrips():
    m = _import_module()
    secret = b"x" * 32
    tok = m.make_signed_token(secret, expires_at=int(time.time()) + 600)
    assert m.verify_signed_token(secret, tok, now=int(time.time())) is True


def test_signed_token_rejected_after_expiry():
    m = _import_module()
    secret = b"x" * 32
    tok = m.make_signed_token(secret, expires_at=1000)
    assert m.verify_signed_token(secret, tok, now=2000) is False


def test_signed_token_rejected_with_wrong_secret():
    m = _import_module()
    tok = m.make_signed_token(b"x" * 32, expires_at=int(time.time()) + 600)
    assert m.verify_signed_token(b"y" * 32, tok, now=int(time.time())) is False


def test_signed_token_tolerates_internal_whitespace():
    """Some chat clients soft-wrap long URLs and inject literal spaces,
    which the URL parser turns into %20. Base64url has no real whitespace,
    so collapsing it on verify is safe and prevents false rejections."""
    m = _import_module()
    secret = b"x" * 32
    tok = m.make_signed_token(secret, expires_at=int(time.time()) + 600)
    head, sig = tok.rsplit(".", 1)
    mangled = head[:10] + "  " + head[10:] + "." + sig  # two spaces
    assert m.verify_signed_token(secret, mangled, now=int(time.time())) is True


def test_signed_token_rejected_when_payload_tampered():
    m = _import_module()
    secret = b"x" * 32
    tok = m.make_signed_token(secret, expires_at=int(time.time()) + 600)
    # Flip a character in the payload portion
    head, sig = tok.rsplit(".", 1)
    bad = head[:-1] + ("a" if head[-1] != "a" else "b") + "." + sig
    assert m.verify_signed_token(secret, bad, now=int(time.time())) is False


# ─── State file lifecycle (single-use Google round-trip) ─────────────


def test_state_file_create_and_consume(tmp_path):
    m = _import_module()
    state_dir = tmp_path / "state"
    state_id = m.create_state_file(state_dir, ttl_s=600)
    # File exists, has reasonable content
    p = state_dir / f"{state_id}.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "created_at" in data and "expires_at" in data
    # Consume succeeds, returns the data, and removes the file
    consumed = m.consume_state_file(state_dir, state_id)
    assert consumed is not None
    assert not p.exists()


def test_state_file_consume_missing_returns_none(tmp_path):
    m = _import_module()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    assert m.consume_state_file(state_dir, "does-not-exist") is None


def test_state_file_consume_expired_returns_none(tmp_path):
    m = _import_module()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_id = "expired-state"
    p = state_dir / f"{state_id}.json"
    p.write_text(json.dumps({
        "created_at": 1000,
        "expires_at": 1500,  # in the past
    }), encoding="utf-8")
    # Should refuse to consume, but still clean up
    assert m.consume_state_file(state_dir, state_id, now=2000) is None
    assert not p.exists()


def test_cleanup_purges_old_state_files(tmp_path):
    m = _import_module()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    fresh = state_dir / "fresh.json"
    stale = state_dir / "stale.json"
    fresh.write_text(json.dumps({"created_at": 1000, "expires_at": 9999}),
                     encoding="utf-8")
    stale.write_text(json.dumps({"created_at": 1, "expires_at": 2}),
                     encoding="utf-8")
    m.cleanup_state_dir(state_dir, now=5000)
    assert fresh.exists()
    assert not stale.exists()


# ─── Google consent URL builder ──────────────────────────────────────


def test_build_google_auth_url_includes_required_params():
    m = _import_module()
    url = m.build_google_auth_url(
        client_id="cid.apps.googleusercontent.com",
        redirect_uri="https://x.ts.net/oauth/callback",
        scopes=["https://www.googleapis.com/auth/calendar"],
        state="xyz",
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid.apps.googleusercontent.com" in url
    # Scopes get %3A-encoded; just check the path fragment is there
    assert "auth%2Fcalendar" in url
    assert "state=xyz" in url
    # Need refresh_token → must request offline access + force consent
    assert "access_type=offline" in url
    assert "prompt=consent" in url


# ─── Token fan-out across workspaces ─────────────────────────────────


def test_fan_out_writes_token_to_every_workspace(tmp_path, monkeypatch):
    m = _import_module()
    # Redirect home to tmp_path so workspace paths resolve under it
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: p.replace("~", str(tmp_path)))

    google_token_response = {
        "access_token": "ya29.fake",
        "refresh_token": "1//fake-refresh",
        "expires_in": 3599,
        "token_type": "Bearer",
        "scope": "https://www.googleapis.com/auth/calendar",
    }

    written = m.fan_out_token(
        google_token_response,
        client_id="cid",
        client_secret="csec",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )

    expected_workspaces = {
        "connector-workspace",
        "family-calendar-workspace",
        "meetings-coach-workspace",
        "shopping-workspace",
    }
    assert {p.parent.name for p in written} == expected_workspaces

    for token_path in written:
        data = json.loads(token_path.read_text(encoding="utf-8"))
        assert data["refresh_token"] == "1//fake-refresh"
        assert data["client_id"] == "cid"
        assert "scopes" in data


def test_fan_out_aborts_when_refresh_token_missing(tmp_path, monkeypatch):
    """Without a refresh_token the new tokens are useless — fail loud."""
    m = _import_module()
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: p.replace("~", str(tmp_path)))
    response_missing_refresh = {
        "access_token": "ya29.fake",
        "expires_in": 3599,
        "scope": "x",
    }
    with pytest.raises(ValueError, match="refresh_token"):
        m.fan_out_token(
            response_missing_refresh,
            client_id="cid",
            client_secret="csec",
            scopes=["x"],
        )


# ─── /oauth/start handler (HMAC gate + state file + Google redirect) ─


def test_start_handler_rejects_missing_token(tmp_path, monkeypatch):
    m = _import_module()
    handler = _make_test_handler(m, tmp_path, monkeypatch)
    status, headers, _body = handler.handle("/oauth/start", {})
    assert status == 400


def test_start_handler_rejects_invalid_token(tmp_path, monkeypatch):
    m = _import_module()
    handler = _make_test_handler(m, tmp_path, monkeypatch)
    status, _h, _b = handler.handle("/oauth/start", {"t": "garbage"})
    assert status in (400, 401, 403)


def test_start_handler_redirects_to_google_on_valid_token(tmp_path, monkeypatch):
    m = _import_module()
    handler = _make_test_handler(m, tmp_path, monkeypatch)
    secret = handler.config["hmac_secret"]
    tok = m.make_signed_token(secret, expires_at=int(time.time()) + 600)
    status, headers, _body = handler.handle("/oauth/start", {"t": tok})
    assert status == 302
    location = dict(headers).get("Location", "")
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")


# ─── /oauth/callback handler (state validation + token exchange) ─────


def test_callback_handler_rejects_unknown_state(tmp_path, monkeypatch):
    m = _import_module()
    handler = _make_test_handler(m, tmp_path, monkeypatch)
    status, _h, _b = handler.handle("/oauth/callback",
                                    {"code": "x", "state": "nope"})
    assert status in (400, 403)


def test_callback_handler_happy_path(tmp_path, monkeypatch):
    m = _import_module()
    handler = _make_test_handler(m, tmp_path, monkeypatch)

    # Establish a pending state as if /oauth/start ran
    state_id = m.create_state_file(handler.config["state_dir"], ttl_s=600)

    # Stub Google's token endpoint
    fake_token_response = {
        "access_token": "ya29.fake",
        "refresh_token": "1//fake-refresh",
        "expires_in": 3599,
        "scope": "https://www.googleapis.com/auth/calendar",
        "token_type": "Bearer",
    }
    monkeypatch.setattr(m, "_exchange_code_for_token",
                        lambda **kw: fake_token_response)

    telegram_calls = []
    monkeypatch.setattr(m, "_send_telegram_confirmation",
                        lambda text: telegram_calls.append(text) or True)

    status, _h, body = handler.handle(
        "/oauth/callback", {"code": "good-code", "state": state_id}
    )
    assert status == 200
    body_text = body.decode("utf-8")
    assert "refreshed" in body_text.lower() or "✅" in body_text

    # State file consumed
    assert not (handler.config["state_dir"] / f"{state_id}.json").exists()

    # Telegram confirmation sent
    assert telegram_calls, "expected one confirmation send"

    # Tokens written to all 4 workspaces under tmp HOME
    home = handler.config["home"]
    for ws in ("connector-workspace", "family-calendar-workspace",
               "meetings-coach-workspace", "shopping-workspace"):
        p = home / ".clawford" / ws / "token.json"
        assert p.exists(), f"missing {p}"


# ─── Test plumbing ────────────────────────────────────────────────────


def _make_test_handler(m, tmp_path, monkeypatch):
    """Build a Handler with tmp dirs + stubbed creds for unit testing.

    Returns an object exposing `.handle(path, query) -> (status, headers, body)`
    that mirrors what the real BaseHTTPRequestHandler does, so tests don't
    have to spin up a real socket.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: p.replace("~", str(home)))
    creds_dir = home / ".clawford" / "fleet-oauth-tailnet-workspace"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials.json").write_text(json.dumps({
        "web": {
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "csec",
            "redirect_uris": ["https://<your-tailscale-host>.tail106e99.ts.net/oauth/callback"],
        }
    }), encoding="utf-8")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    return m.TestHarness(config={
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "csec",
        "redirect_uri": "https://<your-tailscale-host>.tail106e99.ts.net/oauth/callback",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "state_dir": state_dir,
        "hmac_secret": b"k" * 32,
        "home": home,
    })
