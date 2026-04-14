"""Tests for agents/shared/camoufox_proxy.py.

Consolidates the Camoufox + DataImpulse residential proxy scaffolding
used by the Costco daemon (agents/shopping/scripts/costco-token-daemon.py)
and the Amazon browser helper (agents/shopping/scripts/amazon_browser.py).

The proxy provider is DataImpulse, host gw.dataimpulse.com, sticky
session via port 10000. Non-sticky rotating sessions use different
ports per the DataImpulse plan, but the Clawford fleet forces sticky
for auth flows that span multiple requests (Akamai per-subdomain
enforcement rejects a fresh IP for each step).

Two env-var paths are supported:
  - PREMIUM_PROXY_USER + PREMIUM_PROXY_PASS → explicit sticky
    (server = "http://gw.dataimpulse.com:10000")
  - PROXY_URL (or a caller-chosen env var name) → parsed URL. If
    prefer_sticky is True, the port is rewritten to 10000.

Tests use os.environ via monkeypatch (never touch real env).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.shared import camoufox_proxy


# ─── get_proxy_config — PREMIUM path ────────────────────────────────


def test_get_proxy_config_premium_path(monkeypatch):
    monkeypatch.setenv("PREMIUM_PROXY_USER", "premium_abc12345")
    monkeypatch.setenv("PREMIUM_PROXY_PASS", "secret-pass")
    monkeypatch.delenv("PROXY_URL", raising=False)

    cfg = camoufox_proxy.get_proxy_config()

    assert cfg is not None
    assert cfg["server"] == "http://gw.dataimpulse.com:10000"
    assert cfg["username"] == "premium_abc12345"
    assert cfg["password"] == "secret-pass"


def test_get_proxy_config_premium_takes_precedence_over_proxy_url(monkeypatch):
    monkeypatch.setenv("PREMIUM_PROXY_USER", "premium")
    monkeypatch.setenv("PREMIUM_PROXY_PASS", "pp")
    monkeypatch.setenv("PROXY_URL", "http://rot_user:rot_pass@other-host.example.com:823")

    cfg = camoufox_proxy.get_proxy_config()

    assert cfg["username"] == "premium"
    assert cfg["server"] == "http://gw.dataimpulse.com:10000"


# ─── get_proxy_config — PROXY_URL path ──────────────────────────────


def test_get_proxy_config_from_proxy_url_forces_sticky_port_by_default(monkeypatch):
    monkeypatch.delenv("PREMIUM_PROXY_USER", raising=False)
    monkeypatch.delenv("PREMIUM_PROXY_PASS", raising=False)
    monkeypatch.setenv(
        "PROXY_URL",
        "http://rot_user:rot_pass@gw.dataimpulse.com:823",
    )

    cfg = camoufox_proxy.get_proxy_config()

    # Default prefer_sticky=True rewrites port to 10000
    assert cfg is not None
    assert cfg["server"] == "http://gw.dataimpulse.com:10000"
    assert cfg["username"] == "rot_user"
    assert cfg["password"] == "rot_pass"


def test_get_proxy_config_prefer_sticky_false_preserves_port(monkeypatch):
    monkeypatch.delenv("PREMIUM_PROXY_USER", raising=False)
    monkeypatch.delenv("PREMIUM_PROXY_PASS", raising=False)
    monkeypatch.setenv(
        "PROXY_URL",
        "http://u:p@gw.dataimpulse.com:823",
    )

    cfg = camoufox_proxy.get_proxy_config(prefer_sticky=False)

    assert cfg["server"] == "http://gw.dataimpulse.com:823"
    assert cfg["username"] == "u"
    assert cfg["password"] == "p"


def test_get_proxy_config_custom_env_var(monkeypatch):
    monkeypatch.delenv("PREMIUM_PROXY_USER", raising=False)
    monkeypatch.delenv("PROXY_URL", raising=False)
    monkeypatch.setenv(
        "HILDA_PROXY",
        "http://h_user:h_pass@gw.dataimpulse.com:10000",
    )

    cfg = camoufox_proxy.get_proxy_config(env_var="HILDA_PROXY")

    assert cfg is not None
    assert cfg["username"] == "h_user"
    assert cfg["password"] == "h_pass"


def test_get_proxy_config_returns_none_when_no_env_vars(monkeypatch):
    monkeypatch.delenv("PREMIUM_PROXY_USER", raising=False)
    monkeypatch.delenv("PREMIUM_PROXY_PASS", raising=False)
    monkeypatch.delenv("PROXY_URL", raising=False)

    assert camoufox_proxy.get_proxy_config() is None


def test_get_proxy_config_returns_none_when_proxy_url_empty(monkeypatch):
    """Empty string for PROXY_URL is treated the same as unset —
    otherwise the shell-level `export PROXY_URL=` idiom breaks."""
    monkeypatch.delenv("PREMIUM_PROXY_USER", raising=False)
    monkeypatch.setenv("PROXY_URL", "")
    assert camoufox_proxy.get_proxy_config() is None


def test_get_proxy_config_handles_malformed_proxy_url(monkeypatch):
    """A PROXY_URL without auth should return a config with no
    username/password, not crash."""
    monkeypatch.delenv("PREMIUM_PROXY_USER", raising=False)
    monkeypatch.setenv("PROXY_URL", "http://gw.dataimpulse.com:10000")

    cfg = camoufox_proxy.get_proxy_config()
    assert cfg is not None
    assert cfg["server"] == "http://gw.dataimpulse.com:10000"
    assert "username" not in cfg or cfg.get("username") in (None, "")


# ─── launch_camoufox ───────────────────────────────────────────────


def test_launch_camoufox_passes_proxy_config_through(monkeypatch):
    captured = {}

    class FakeCamoufoxInstance:
        def __enter__(self):
            return MagicMock()
        def __exit__(self, *a):
            return None

    def fake_camoufox_ctor(**kwargs):
        captured.update(kwargs)
        return FakeCamoufoxInstance()

    monkeypatch.setattr(camoufox_proxy, "_Camoufox", fake_camoufox_ctor)

    proxy_cfg = {
        "server": "http://gw.dataimpulse.com:10000",
        "username": "u",
        "password": "p",
    }
    with camoufox_proxy.launch_camoufox(proxy_cfg, width=1920, height=1080) as _browser:
        pass

    assert captured["proxy"] == proxy_cfg
    assert captured["geoip"] is True  # proxy_cfg is set → geoip on
    # humanize=False is the Clawford fleet default — randomized
    # fingerprints trigger Amazon MFA on every launch (see amazon_browser.py)
    assert captured["humanize"] is False
    # Config should pin the viewport to width/height
    config = captured.get("config") or {}
    assert config.get("window.outerWidth") == 1920
    assert config.get("window.outerHeight") == 1080


def test_launch_camoufox_geoip_off_when_no_proxy(monkeypatch):
    """geoip should only be enabled when a proxy is present. Passing
    None as the proxy_cfg yields geoip=False and proxy=None."""
    captured = {}

    class FakeInstance:
        def __enter__(self):
            return MagicMock()
        def __exit__(self, *a):
            return None

    monkeypatch.setattr(
        camoufox_proxy,
        "_Camoufox",
        lambda **kw: captured.update(kw) or FakeInstance(),
    )

    with camoufox_proxy.launch_camoufox(None) as _browser:
        pass

    assert captured["proxy"] is None
    assert captured["geoip"] is False


def test_launch_camoufox_default_viewport_is_1280x800(monkeypatch):
    captured = {}
    class FakeInstance:
        def __enter__(self):
            return MagicMock()
        def __exit__(self, *a):
            return None

    monkeypatch.setattr(
        camoufox_proxy,
        "_Camoufox",
        lambda **kw: captured.update(kw) or FakeInstance(),
    )

    with camoufox_proxy.launch_camoufox(None) as _:
        pass

    config = captured["config"]
    assert config["window.outerWidth"] == 1280
    assert config["window.outerHeight"] == 800


# ─── Constants ──────────────────────────────────────────────────────


def test_dataimpulse_constants_are_exported():
    """The host + sticky port should be named constants, not magic
    numbers — callers (and future migrations) need to reference them."""
    assert camoufox_proxy.DATAIMPULSE_HOST == "gw.dataimpulse.com"
    assert camoufox_proxy.DATAIMPULSE_STICKY_PORT == 10000
