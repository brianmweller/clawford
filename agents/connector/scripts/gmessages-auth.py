#!/usr/bin/env python3
"""gmessages-auth.py — Google sign-in for Google Messages Web (Camoufox).

The chromium build can't get past Google's `signin/rejected` wall
because `--enable-automation` removal alone leaves enough
fingerprint signals (`navigator.webdriver`, plugin list, TLS hello)
for Google to refuse the password page entirely. Camoufox
(privacy-patched Firefox) sails through.

Flow:
  1. Launch Camoufox with `persistent_context=True` and a profile
     dir under the connector workspace, so cookies/localStorage
     survive across runs (the periodic mine reads the same dir).
  2. Navigate to messages.google.com → click Sign In.
  3. Fill email (hard-coded to sam.smith@example.com — the
     account linked to the operator's Android Messages app).
  4. Fill password from $GMESSAGES_GOOGLE_PASSWORD or stdin getpass.
  5. Screenshot the 2FA page (number-match prompt) so the operator can
     confirm on his phone.
  6. Poll the page URL every 3s for up to 4 minutes — exits as soon
     as it lands on /web/conversations.

Run interactively (or via stored env var):

    ssh -i ~/.ssh/id_ed25519 openclaw@203.0.113.10
    cd ~/openclaw
    docker compose exec openclaw-gateway \\
        python3 /home/openclaw/.clawford/connector-workspace/scripts/gmessages-auth.py

Profile path:        ~/.clawford/connector-workspace/gmessages-profile/
2FA screenshot path: ~/Dropbox/openclaw-backup/tmp/gmessages-2fa.png
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import time
import traceback
from pathlib import Path


# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break


PROFILE_DIR = Path(
    os.path.expanduser("~/.clawford/connector-workspace/gmessages-profile")
)
TFA_SCREENSHOT = Path(
    os.path.expanduser("~/Dropbox/openclaw-backup/tmp/gmessages-2fa.png")
)
SUCCESS_URL_FRAGMENT = "/conversations"  # matches /web/conversations and /web/u/0/conversations
GOOGLE_EMAIL = "sam.smith@example.com"
SIGNIN_LANDING = "https://messages.google.com/web/welcome"
PAGE_LOAD_TIMEOUT_MS = 60_000
PASSWORD_SETTLE_MS = 4_000
POLL_INTERVAL_SEC = 3
POLL_TIMEOUT_SEC = 240


def _read_password() -> str:
    pw_env = os.environ.get("GMESSAGES_GOOGLE_PASSWORD")
    if pw_env:
        return pw_env
    if not sys.stdin.isatty():
        raise RuntimeError(
            "No GMESSAGES_GOOGLE_PASSWORD env var and no TTY for getpass; "
            "run interactively under `docker compose exec`."
        )
    return getpass.getpass(f"Google password for {GOOGLE_EMAIL}: ")


def run() -> dict:
    from agents.shared.camoufox_proxy import launch_camoufox

    password = _read_password()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    TFA_SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)

    print("=== gmessages-auth (Camoufox + Google sign-in) ===", file=sys.stderr)
    print(f"Profile:    {PROFILE_DIR}", file=sys.stderr)
    print(f"Screenshot: {TFA_SCREENSHOT}", file=sys.stderr)
    print("Launching Camoufox (Firefox-based, anti-detection)...", file=sys.stderr)

    # headless='virtual' = Camoufox auto-starts a virtual X display
    # (no manual Xvfb wiring needed). Real headless mode would expose
    # different chromium flags Google reads, so 'virtual' is the right
    # middle ground for a no-real-display VPS.
    with launch_camoufox(
        proxy_cfg=None,
        headless="virtual",
        os_name="windows",
        persistent_context=True,
        user_data_dir=PROFILE_DIR,
    ) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SIGNIN_LANDING, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_timeout(3_000)

        clicked = False
        for sel in ("text=Sign In", "text=Sign in", "[aria-label*='Sign in']"):
            try:
                page.click(sel, timeout=5_000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            return {
                "status": "error",
                "alert": "🐛 gmessages-auth: couldn't find Sign In button on /web/welcome",
                "url": page.url,
            }

        page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_timeout(2_500)
        print("Filling email...", file=sys.stderr)

        try:
            page.fill('input[type="email"]', GOOGLE_EMAIL, timeout=10_000)
            page.press('input[type="email"]', "Enter")
        except Exception as e:
            try:
                page.screenshot(path=str(TFA_SCREENSHOT), full_page=True)
            except Exception:
                pass
            return {
                "status": "error",
                "alert": f"🐛 gmessages-auth: email step failed: {e}",
                "url": page.url,
                "screenshot": str(TFA_SCREENSHOT),
            }

        page.wait_for_timeout(3_500)
        print("Filling password...", file=sys.stderr)

        try:
            page.fill('input[type="password"]', password, timeout=15_000)
            page.press('input[type="password"]', "Enter")
        except Exception as e:
            try:
                page.screenshot(path=str(TFA_SCREENSHOT), full_page=True)
            except Exception:
                pass
            return {
                "status": "error",
                "alert": f"🐛 gmessages-auth: password step failed: {e}",
                "url": page.url,
                "screenshot": str(TFA_SCREENSHOT),
            }

        page.wait_for_timeout(PASSWORD_SETTLE_MS)
        page.screenshot(path=str(TFA_SCREENSHOT), full_page=True)
        print(
            f"\n2FA screenshot saved to {TFA_SCREENSHOT}\n"
            f"Open it on your laptop (Dropbox sync) and confirm any\n"
            f"matching number/picture in your Google app on Android.\n",
            file=sys.stderr,
        )
        print(
            f"Polling page URL every {POLL_INTERVAL_SEC}s for up to "
            f"{POLL_TIMEOUT_SEC}s — exits as soon as /web/conversations "
            f"loads.",
            file=sys.stderr,
        )

        start = time.monotonic()
        paired = False
        last_url = ""
        while time.monotonic() - start < POLL_TIMEOUT_SEC:
            current = page.url or ""
            if current != last_url:
                print(f"  url -> {current}", file=sys.stderr)
                last_url = current
            if SUCCESS_URL_FRAGMENT in current:
                paired = True
                break
            page.wait_for_timeout(POLL_INTERVAL_SEC * 1_000)
            try:
                page.screenshot(path=str(TFA_SCREENSHOT), full_page=True)
            except Exception:
                pass

        if not paired:
            return {
                "status": "degraded",
                "alert": (
                    f"🐛 gmessages-auth: didn't reach {SUCCESS_URL_FRAGMENT} "
                    f"within {POLL_TIMEOUT_SEC}s — last URL: {last_url}"
                ),
                "screenshot": str(TFA_SCREENSHOT),
            }

        # Settle so cookies/localStorage flush before the context closes
        page.wait_for_timeout(8_000)

    return {
        "status": "ok",
        "message": "Signed in. Run gmessages-mine.py to scrape conversations.",
        "profile": str(PROFILE_DIR),
    }


def main() -> int:
    try:
        result = run()
    except Exception as e:
        result = {
            "status": "error",
            "error": str(e),
            "alert": f"🐛 gmessages-auth crashed: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
