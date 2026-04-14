"""camoufox_proxy — Camoufox + DataImpulse residential proxy helpers.

Consolidates the proxy-config + launch scaffolding duplicated across:
  - agents/shopping/scripts/costco-token-daemon.py
  - agents/shopping/scripts/costco-capture-token.py
  - agents/shopping/scripts/costco-capture-session.py
  - agents/shopping/scripts/amazon-auth.py
  - agents/shopping/scripts/amazon_browser.py

The proxy provider is **DataImpulse** (host `gw.dataimpulse.com`).
Port 10000 is the premium sticky-session port: the same egress IP is
reused across a sequence of requests, which Akamai requires for
per-subdomain flows like the Costco B2C signin sequence (fresh-IP
rotation triggers 403 Access Denied on signin.costco.com).
Non-sticky/rotating sessions would bind to a different port per the
DataImpulse plan — the fleet forces sticky for every auth flow.

Two env-var paths are supported:

  PREMIUM_PROXY_USER + PREMIUM_PROXY_PASS
      Highest precedence. Canonical sticky mode — the username is an
      opaque token from the DataImpulse dashboard and the server is
      hardcoded to the sticky port.

  PROXY_URL (or a caller-chosen env var)
      Fallback. A URL in http://user:pass@host:port form. If
      prefer_sticky=True (the default), the port component is rewritten
      to DATAIMPULSE_STICKY_PORT (10000) so the caller can't
      accidentally route through the rotating port for an auth flow.

Related memory entries: feedback_costco_daemon.md,
feedback_costco_silent_refresh.md, feedback_b2c_setcookie_bug.md.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlparse


DATAIMPULSE_HOST = "gw.dataimpulse.com"
DATAIMPULSE_STICKY_PORT = 10000

# humanize=False + fixed config = consistent device fingerprint.
# Amazon grants MFA exemption per device; randomized fingerprints
# trigger MFA on every launch. Same concern for the Costco daemon.
_CLAWFORD_HUMANIZE = False


def _Camoufox(**kwargs):  # pragma: no cover — replaced in tests
    from camoufox.sync_api import Camoufox  # type: ignore
    return Camoufox(**kwargs)


def get_proxy_config(
    env_var: str = "PROXY_URL",
    *,
    prefer_sticky: bool = True,
) -> dict | None:
    """Return a Camoufox/Playwright proxy-config dict, or None if no
    proxy is configured in env.

    Priority:
      1. PREMIUM_PROXY_USER + PREMIUM_PROXY_PASS → sticky mode, server
         pinned to http://<DATAIMPULSE_HOST>:<DATAIMPULSE_STICKY_PORT>.
      2. <env_var> (default "PROXY_URL") → parsed URL, port rewritten
         to the sticky port if prefer_sticky.
      3. None.

    The returned dict has `server`, and when credentials are present,
    `username` and `password`. Its shape matches Camoufox's `proxy=`
    parameter and Playwright's `proxy=` parameter.
    """
    premium_user = os.environ.get("PREMIUM_PROXY_USER", "")
    premium_pass = os.environ.get("PREMIUM_PROXY_PASS", "")
    if premium_user and premium_pass:
        return {
            "server": f"http://{DATAIMPULSE_HOST}:{DATAIMPULSE_STICKY_PORT}",
            "username": premium_user,
            "password": premium_pass,
        }

    proxy_url = os.environ.get(env_var, "")
    if not proxy_url:
        return None

    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return None

    scheme = parsed.scheme or "http"
    port = (
        DATAIMPULSE_STICKY_PORT
        if prefer_sticky
        else (parsed.port or DATAIMPULSE_STICKY_PORT)
    )
    cfg: dict[str, Any] = {
        "server": f"{scheme}://{parsed.hostname}:{port}",
    }
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def _viewport_config(width: int, height: int) -> dict:
    """The Camoufox `config` dict that pins window + screen size.

    Mirrors the shape used in amazon_browser.py and
    costco-token-daemon.py — Camoufox takes a flat dict of
    `browser-property:value` overrides, not a nested structure.
    """
    return {
        "window.outerWidth": width,
        "window.outerHeight": height,
        "window.innerWidth": width,
        "window.innerHeight": height,
        "screen.width": width,
        "screen.height": height,
        "screen.availWidth": width,
        "screen.availHeight": height - 45,
        "screen.colorDepth": 24,
        "screen.pixelDepth": 24,
        "mediaDevices:enabled": True,
    }


@contextmanager
def launch_camoufox(
    proxy_cfg: dict | None,
    *,
    width: int = 1280,
    height: int = 800,
    headless: bool = False,
    os_name: str = "windows",
    extra_config: dict | None = None,
) -> Iterator[Any]:
    """Context manager yielding a Camoufox browser instance.

    `proxy_cfg` is the dict returned by get_proxy_config (or None for
    a bare-IP launch). `geoip` is toggled automatically based on
    whether proxy_cfg is set.

    `extra_config` is merged into the default viewport config so
    individual callers can override specific Camoufox properties
    (e.g. the Costco daemon's AudioContext overrides).
    """
    config = _viewport_config(width, height)
    if extra_config:
        config.update(extra_config)

    instance = _Camoufox(
        headless=headless,
        proxy=proxy_cfg,
        geoip=bool(proxy_cfg),
        os=os_name,
        humanize=_CLAWFORD_HUMANIZE,
        config=config,
        i_know_what_im_doing=True,
    )
    with instance as browser:
        yield browser
