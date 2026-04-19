"""Tests for the B2C SSO-cookie filter in costco-token-daemon.py.

Why this exists: the "scheduled reauth" path was supposed to reset
Azure B2C's ~72h absolute refresh-token cap by forcing a fresh
interactive authentication every 65h. But because it was injecting
x-ms-cpim-*, kmsi*, and sso-* cookies into the Camoufox context,
B2C completed the auth via SSO — no credential challenge, no new
session, no cap reset. Tokens kept getting issued from the ORIGINAL
(stale) grant-issued timestamp and eventually showed up with absurd
97-second TTLs right before the cap hit.

filter_session_cookies(cookies, drop_b2c_session=False) is the pure
helper the reauth function calls when it wants to force fresh.

Run: cd agents/shared && python3 -m pytest tests/test_reauth_cookie_filter.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DAEMON_PATH = REPO_ROOT / "agents" / "shopping" / "scripts" / "costco-token-daemon.py"


def _load_daemon_module():
    # Stub heavy optional deps so the module import doesn't explode in CI.
    sys.modules.setdefault("camoufox", type(sys)("camoufox"))
    sys.modules.setdefault("camoufox.sync_api", type(sys)("camoufox.sync_api"))
    sys.modules.setdefault("curl_cffi", type(sys)("curl_cffi"))
    sys.modules.setdefault("curl_cffi.requests", type(sys)("curl_cffi.requests"))
    spec = importlib.util.spec_from_file_location("costco_token_daemon", DAEMON_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["costco_token_daemon"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def daemon():
    return _load_daemon_module()


def _cookie(name: str, domain: str = ".costco.com") -> dict:
    return {"name": name, "domain": domain, "value": "x" * 32}


def test_filter_always_drops_wc_prefix(daemon):
    cookies = [_cookie("WC_PERSISTENT"), _cookie("WC_AUTHENTICATION_98765"), _cookie("bm_sz")]
    out = daemon.filter_session_cookies(cookies, drop_b2c_session=False)
    names = [c["name"] for c in out]
    assert names == ["bm_sz"]


def test_filter_keeps_b2c_cookies_by_default(daemon):
    cookies = [
        _cookie("x-ms-cpim-sso:costcoauth.onmicrosoft.com_0"),
        _cookie("kmsi"),
        _cookie("sso-pharmacy-session"),
        _cookie("bm_sz"),
    ]
    out = daemon.filter_session_cookies(cookies, drop_b2c_session=False)
    names = {c["name"] for c in out}
    assert "x-ms-cpim-sso:costcoauth.onmicrosoft.com_0" in names
    assert "kmsi" in names
    assert "sso-pharmacy-session" in names
    assert "bm_sz" in names


def test_filter_drops_b2c_when_forced(daemon):
    cookies = [
        _cookie("x-ms-cpim-sso:costcoauth.onmicrosoft.com_0"),
        _cookie("x-ms-cpim-csrf"),
        _cookie("x-ms-cpim-geo"),
        _cookie("kmsi"),
        _cookie("kmsi_70b628675a5fc88e4b899179f6762e97d4880deb509e99ca4fde07dd29128413"),
        _cookie("sso-pharmacy-session"),
        _cookie("bm_sz"),
        _cookie("ak_bmsc"),
        _cookie("_abck"),
        _cookie("WC_PERSISTENT"),
    ]
    out = daemon.filter_session_cookies(cookies, drop_b2c_session=True)
    names = {c["name"] for c in out}
    # Akamai cookies must survive — we still need bot-bypass on the signin flow.
    assert "bm_sz" in names
    assert "ak_bmsc" in names
    assert "_abck" in names
    # B2C session identifiers must be dropped — they're the reason the reauth
    # was silently completing via SSO without a real credential login.
    assert not any(n.startswith("x-ms-cpim") for n in names)
    assert not any(n.startswith("kmsi") for n in names)
    assert not any("sso" in n.lower() for n in names)
    # WC_ still dropped unconditionally.
    assert not any(n.startswith("WC_") for n in names)


def test_filter_preserves_order(daemon):
    # Cookies should keep relative ordering; some Akamai challenges are
    # sensitive to the order the browser replays them.
    cookies = [
        _cookie("ak_bmsc"),
        _cookie("x-ms-cpim-csrf"),  # dropped when forced
        _cookie("bm_sz"),
        _cookie("kmsi"),            # dropped when forced
        _cookie("_abck"),
    ]
    out = daemon.filter_session_cookies(cookies, drop_b2c_session=True)
    assert [c["name"] for c in out] == ["ak_bmsc", "bm_sz", "_abck"]


def test_filter_handles_missing_name(daemon):
    # Defensive: malformed cookie without a name should be dropped, not crash.
    cookies = [
        _cookie("ak_bmsc"),
        {"domain": ".costco.com", "value": "x"},  # no name
        _cookie("x-ms-cpim-sso"),
    ]
    out = daemon.filter_session_cookies(cookies, drop_b2c_session=True)
    names = [c.get("name", "") for c in out]
    assert names == ["ak_bmsc"]
