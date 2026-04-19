"""Tests for _verify_fresh_grant() in costco-token-daemon.py.

The function decodes the freshly-saved id_token and checks `auth_time`
(seconds-since-epoch of the last real credential authentication).
Stale auth_time means SSO carried through — Azure's 72h absolute cap
was NOT reset.

Note: do not use exp-iat as a signal — Costco always issues 15-minute
id_tokens regardless of freshness.

Run: cd agents/shared && python3 -m pytest tests/test_reauth_verify_fresh_grant.py -v
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DAEMON_PATH = REPO_ROOT / "agents" / "shopping" / "scripts" / "costco-token-daemon.py"


def _load_daemon_module():
    sys.modules.setdefault("camoufox", type(sys)("camoufox"))
    sys.modules.setdefault("camoufox.sync_api", type(sys)("camoufox.sync_api"))
    sys.modules.setdefault("curl_cffi", type(sys)("curl_cffi"))
    sys.modules.setdefault("curl_cffi.requests", type(sys)("curl_cffi.requests"))
    spec = importlib.util.spec_from_file_location("costco_token_daemon", DAEMON_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["costco_token_daemon"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    m = _load_daemon_module()
    token_file = tmp_path / "costco-tokens.json"
    monkeypatch.setattr(m, "TOKEN_FILE", str(token_file))
    monkeypatch.setattr(m, "WORKSPACE", str(tmp_path))
    return m


def _mint_jwt(auth_time: int | None, iat: int | None = None, exp: int | None = None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    now = int(time.time())
    claims = {
        "iat": iat if iat is not None else now,
        "exp": exp if exp is not None else now + 900,
        "sub": "test",
    }
    if auth_time is not None:
        claims["auth_time"] = auth_time
    payload_bytes = json.dumps(claims).encode()
    payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _write_tokens(path: str, id_token: str) -> None:
    with open(path, "w") as f:
        json.dump({"id_token": id_token, "refresh_token": "opaque"}, f)


def test_fresh_auth_time_passes(daemon):
    now = int(time.time())
    _write_tokens(daemon.TOKEN_FILE, _mint_jwt(auth_time=now - 30))  # 30s ago
    sent = []
    assert daemon._verify_fresh_grant(notifier=sent.append) is True
    assert sent == []


def test_stale_auth_time_fails_and_notifies(daemon):
    now = int(time.time())
    # Mirrors the 2026-04-19 real-world case: token's auth_time was from
    # an earlier residential flow 46 min before the scheduled reauth.
    _write_tokens(daemon.TOKEN_FILE, _mint_jwt(auth_time=now - 46 * 60))
    sent = []
    result = daemon._verify_fresh_grant(notifier=sent.append)
    assert result is False
    assert len(sent) == 1
    # Message should talk about auth_time and cap, not about TTL.
    msg = sent[0]
    assert "auth_time" in msg
    assert "72h" in msg


def test_auth_time_at_window_edge_passes(daemon):
    now = int(time.time())
    _write_tokens(daemon.TOKEN_FILE, _mint_jwt(auth_time=now - 300))
    assert daemon._verify_fresh_grant(max_auth_age_s=300) is True


def test_auth_time_one_second_over_fails(daemon):
    now = int(time.time())
    _write_tokens(daemon.TOKEN_FILE, _mint_jwt(auth_time=now - 301))
    assert daemon._verify_fresh_grant(max_auth_age_s=300) is False


def test_normal_900s_id_token_ttl_does_not_trigger_false_positive(daemon):
    # Regression: the previous version of _verify_fresh_grant checked
    # exp-iat of the id_token and would falsely complain about every
    # successful reauth (Costco always issues 15-min id_tokens).
    # With auth_time-based checking, a fresh 900s-TTL token passes.
    now = int(time.time())
    token = _mint_jwt(auth_time=now - 10, iat=now, exp=now + 900)
    _write_tokens(daemon.TOKEN_FILE, token)
    assert daemon._verify_fresh_grant() is True


def test_missing_auth_time_claim_returns_true(daemon):
    # If Azure doesn't emit auth_time (policy-dependent), fall back to
    # "can't verify, don't block the flow."
    now = int(time.time())
    _write_tokens(daemon.TOKEN_FILE, _mint_jwt(auth_time=None, iat=now, exp=now + 900))
    assert daemon._verify_fresh_grant() is True


def test_non_jwt_token_returns_true(daemon):
    _write_tokens(daemon.TOKEN_FILE, "not-a-jwt-value")
    assert daemon._verify_fresh_grant() is True


def test_missing_token_file_returns_true(daemon):
    assert daemon._verify_fresh_grant() is True


def test_corrupt_token_returns_true(daemon):
    with open(daemon.TOKEN_FILE, "w") as f:
        f.write("not json")
    assert daemon._verify_fresh_grant() is True
