"""Tests for _verify_fresh_grant() in costco-token-daemon.py.

The function decodes the freshly-saved id_token and checks whether
its exp-iat TTL is long enough to prove Azure reset the absolute cap.
Short TTL (e.g. 97s) means SSO carried through — cap NOT reset.

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


def _mint_jwt(iat: int, exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_bytes = json.dumps({"iat": iat, "exp": exp, "sub": "test"}).encode()
    payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    sig = "sig"
    return f"{header}.{payload}.{sig}"


def _write_tokens(path: str, id_token: str) -> None:
    with open(path, "w") as f:
        json.dump({"id_token": id_token, "refresh_token": "dummy"}, f)


def test_long_ttl_token_passes(daemon):
    now = int(time.time())
    token = _mint_jwt(iat=now, exp=now + 3600)  # 1 hour TTL
    _write_tokens(daemon.TOKEN_FILE, token)

    sent = []
    assert daemon._verify_fresh_grant(notifier=sent.append) is True
    assert sent == []  # no nudge


def test_short_ttl_token_fails_and_notifies(daemon):
    now = int(time.time())
    token = _mint_jwt(iat=now, exp=now + 97)  # the real-world bad case
    _write_tokens(daemon.TOKEN_FILE, token)

    sent = []
    result = daemon._verify_fresh_grant(notifier=sent.append)
    assert result is False
    assert len(sent) == 1
    assert "97" in sent[0] or "short" in sent[0].lower()


def test_ttl_right_at_threshold_passes(daemon):
    now = int(time.time())
    token = _mint_jwt(iat=now, exp=now + 3600)
    _write_tokens(daemon.TOKEN_FILE, token)
    assert daemon._verify_fresh_grant(min_ttl_s=3600) is True


def test_ttl_one_second_below_threshold_fails(daemon):
    now = int(time.time())
    token = _mint_jwt(iat=now, exp=now + 3599)
    _write_tokens(daemon.TOKEN_FILE, token)
    assert daemon._verify_fresh_grant(min_ttl_s=3600) is False


def test_non_jwt_token_returns_true(daemon):
    # Opaque tokens shouldn't block the flow.
    _write_tokens(daemon.TOKEN_FILE, "not-a-jwt-value")
    assert daemon._verify_fresh_grant() is True


def test_missing_token_file_returns_true(daemon):
    # If we can't verify (e.g. race), don't fail the flow.
    assert daemon._verify_fresh_grant() is True


def test_corrupt_token_returns_true(daemon):
    with open(daemon.TOKEN_FILE, "w") as f:
        f.write("not json")
    assert daemon._verify_fresh_grant() is True
