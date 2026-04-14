#!/usr/bin/env python3
"""gmessages-auth.py — One-time Google Messages Web pairing flow.

Follows the same xvfb + non-headless Chromium + remote-debugging-port
pattern as news-digest/linkedin-auth.py, adapted for messages.google.com.
The pairing is done ONCE by the operator over an SSH tunnel; afterwards
gmessages-mine.py runs headlessly against the persistent profile.

Setup (run from the operator's laptop):
    Terminal 1: ssh -L 9222:localhost:9222 -i ~/.ssh/id_ed25519 openclaw@203.0.113.10
    Terminal 2 (on VPS via ssh): cd ~/openclaw && docker compose exec openclaw-gateway \\
        python3 /home/node/.openclaw/connector-workspace/scripts/gmessages-auth.py
    Browser: open chrome://inspect/#devices, Configure, add localhost:9222,
             then click `inspect` on the "Messages" target.
    Phone: open Google Messages on Android → three-dot menu → Device pairing →
           scan the QR code shown in the DevTools target.

After the QR is accepted, the conversation list renders. Press Enter in
the SSH terminal to shut down Chromium gracefully and commit the profile
to disk. Subsequent gmessages-mine.py runs use the saved session.

Profile: ~/.openclaw/connector-workspace/gmessages-profile/
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.playwright_profile import (
    cleanup_profile_lock,
    ensure_profile_dir,
    ensure_xvfb,
)

PROFILE_DIR = Path(
    os.path.expanduser("~/.openclaw/connector-workspace/gmessages-profile")
)
DEBUG_PORT = 9228  # different from LinkedIn's 9223 so both can coexist
TUNNEL_PORT = 9222


def main() -> int:
    ensure_profile_dir(PROFILE_DIR)
    removed = cleanup_profile_lock(PROFILE_DIR)
    if removed:
        print(f"Cleaned stale locks: {', '.join(removed)}")

    # Kill any prior instances so the debug port is free
    subprocess.run(["pkill", "-f", "chromium.*gmessages-profile"], capture_output=True)
    subprocess.run(["pkill", "-f", "socat.*:9228"], capture_output=True)
    time.sleep(1)

    print(f"Profile dir: {PROFILE_DIR}")
    print("Launching non-headless Chromium via xvfb with remote debugging...")
    print()

    xvfb = ensure_xvfb(display_num=99)
    time.sleep(1)

    chrome = subprocess.Popen(
        [
            "/usr/bin/chromium",
            f"--remote-debugging-port={DEBUG_PORT}",
            "--no-sandbox",
            "--disable-gpu",
            "--no-first-run",
            "--disable-extensions",
            "--window-size=1280,900",
            f"--user-data-dir={PROFILE_DIR}",
            "https://messages.google.com/web/authentication",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(3)

    if chrome.poll() is not None:
        if xvfb is not None:
            xvfb.terminate()
        print("ERROR: Chromium failed to start.")
        return 1

    socat = subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{TUNNEL_PORT},fork,reuseaddr,bind=0.0.0.0",
            f"TCP:127.0.0.1:{DEBUG_PORT}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(1)

    print("Chromium is running. Pair your Android phone:")
    print()
    print("  1. On your laptop, tunnel is already up at localhost:9222")
    print("  2. Open chrome://inspect/#devices")
    print("  3. Click Configure, add localhost:9222")
    print("  4. Click 'inspect' on the Messages target")
    print("  5. On Android: Messages → menu → Device pairing → QR scanner")
    print("  6. Scan the QR code, check 'Remember this computer'")
    print("  7. Once the conversation list renders, come back here")
    print("  8. Press Enter to save the session and exit")
    print()

    try:
        input("Press Enter when pairing is complete...")
    except EOFError:
        pass

    socat.terminate()
    chrome.terminate()
    try:
        chrome.wait(timeout=5)
    except subprocess.TimeoutExpired:
        chrome.kill()
    if xvfb is not None:
        xvfb.terminate()

    print()
    print(f"Session saved to {PROFILE_DIR}")
    print("gmessages-mine.py will now use this profile headlessly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
