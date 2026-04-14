#!/usr/bin/env python3
"""gmessages-auth.py — One-time Google Messages Web pairing flow.

Drives a non-headless Chromium under Xvfb (so the QR code renders to a
real pixel buffer), navigates to messages.google.com/web/authentication,
screenshots the QR to a host-visible path under the openclaw-backup
Dropbox, and polls for successful pairing — then exits cleanly so the
persistent profile is committed to disk.

No debug port / SSH tunnel / chrome://inspect required. the operator scans the
screenshot with his Android Messages app (Messages menu → Device pairing
→ QR scanner) within the 2-minute poll window.

Setup:
    1. SSH to VPS: ssh -i ~/.ssh/id_ed25519 openclaw@203.0.113.10
    2. Run inside the gateway container:
         cd ~/openclaw && docker compose exec openclaw-gateway \\
             python3 /home/node/.openclaw/connector-workspace/scripts/gmessages-auth.py
    3. Script writes /home/node/Dropbox/openclaw-backup/tmp/gmessages-qr.png
       (= host path /home/openclaw/Dropbox/openclaw-backup/tmp/gmessages-qr.png,
       auto-synced to the operator's laptop via Dropbox within seconds)
    4. the operator opens the file on his laptop, scans with Android Messages
    5. Script polls for ~2 minutes and exits as soon as the page URL
       changes away from /authentication (indicating successful pair)
    6. Subsequent gmessages-mine.py runs use the saved profile headlessly

Profile path: ~/.openclaw/connector-workspace/gmessages-profile/
QR screenshot: /home/node/Dropbox/openclaw-backup/tmp/gmessages-qr.png
"""
from __future__ import annotations

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
    os.path.expanduser("~/.openclaw/connector-workspace/gmessages-profile")
)
QR_SCREENSHOT = Path(
    os.path.expanduser("~/Dropbox/openclaw-backup/tmp/gmessages-qr.png")
)
AUTH_URL = "https://messages.google.com/web/authentication"
POST_LOAD_SETTLE_MS = 5_000
POLL_INTERVAL_SEC = 3
POLL_TIMEOUT_SEC = 180  # three minutes — Google refreshes the QR every ~60s


def run() -> dict:
    from agents.shared.playwright_profile import (
        ensure_profile_dir,
        cleanup_profile_lock,
        ensure_xvfb,
        launch_persistent_profile,
    )

    ensure_profile_dir(PROFILE_DIR)
    cleanup_profile_lock(PROFILE_DIR)
    QR_SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)

    # Xvfb is required even though Playwright's headful mode wants a
    # display — the gateway container has no real X server.
    ensure_xvfb(display_num=99)

    print("=== gmessages-auth ===", file=sys.stderr)
    print(f"Profile:    {PROFILE_DIR}", file=sys.stderr)
    print(f"Screenshot: {QR_SCREENSHOT}", file=sys.stderr)
    print("Launching Chromium under Xvfb...", file=sys.stderr)

    with launch_persistent_profile(PROFILE_DIR, headless=False) as browser:
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(POST_LOAD_SETTLE_MS)

        page.screenshot(path=str(QR_SCREENSHOT), full_page=True)
        print(f"QR screenshot saved to {QR_SCREENSHOT}", file=sys.stderr)
        print(
            "Open the file on your laptop (Dropbox sync) and scan with "
            "Android Messages → menu → Device pairing → QR scanner.",
            file=sys.stderr,
        )
        print(
            f"Polling page URL every {POLL_INTERVAL_SEC}s for "
            f"{POLL_TIMEOUT_SEC}s — will exit as soon as pairing completes.",
            file=sys.stderr,
        )

        start = time.monotonic()
        paired = False
        while time.monotonic() - start < POLL_TIMEOUT_SEC:
            current = page.url or ""
            if "authentication" not in current:
                paired = True
                break
            # If the QR expired, Google re-renders it on the same URL.
            # Refresh the screenshot so the operator can re-scan the new one.
            page.wait_for_timeout(POLL_INTERVAL_SEC * 1_000)
            try:
                page.screenshot(path=str(QR_SCREENSHOT), full_page=True)
            except Exception:
                pass

        if not paired:
            return {
                "status": "degraded",
                "alert": (
                    "🐛 gmessages-auth: QR not scanned within "
                    f"{POLL_TIMEOUT_SEC}s — re-run the script to retry"
                ),
                "qr_screenshot": str(QR_SCREENSHOT),
            }

        # One more settle to give the conversation list a chance to
        # populate before we close — ensures cookies/localStorage are
        # fully written into the persistent profile.
        page.wait_for_timeout(5_000)

    return {
        "status": "ok",
        "message": "Pairing saved. Run gmessages-mine.py to scrape.",
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
