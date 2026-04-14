#!/usr/bin/env python3
"""
gcal-auth.py — One-time OAuth2 setup for Google Calendar API.

Run this interactively on the VPS (via SSH tunnel on port 8080) or
locally before SCPing token.json to the VPS. Authorizes the agent to
read family Google Calendars.

Usage: python3 gcal-auth.py [--credentials PATH] [--token PATH]

Defaults:
  --credentials: ~/.openclaw/family-calendar-workspace/credentials.json
  --token:       ~/.openclaw/family-calendar-workspace/token.json

Prerequisites:
  1. Google Cloud Console: create project, enable Calendar API + Gmail API
  2. Create OAuth2 credentials (Desktop app type)
  3. Download credentials.json to the workspace
  4. Add the target Google account as a test user on the OAuth consent
     screen BEFORE first auth (otherwise the flow fails with "Access
     blocked: app has not completed verification")

After running, token.json will contain a refresh token that auto-renews.
Re-run this script only if the token is revoked or the Google Cloud
project changes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- shared library sys.path shim ---
# Find the first ancestor containing agents/shared/ and prepend it to
# sys.path so `from agents.shared import X` resolves in both the local
# repo layout and the deployed <workspace>/agents/shared/ layout.
# See agents/shared/deploy.py::sync_shared_library.
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.google_oauth import build_flow, save_credentials

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]

DEFAULT_CREDS = os.path.expanduser(
    "~/.openclaw/family-calendar-workspace/credentials.json"
)
DEFAULT_TOKEN = os.path.expanduser(
    "~/.openclaw/family-calendar-workspace/token.json"
)


def parse_args() -> tuple[str, str]:
    creds_path = DEFAULT_CREDS
    token_path = DEFAULT_TOKEN
    for i, arg in enumerate(sys.argv):
        if arg == "--credentials" and i + 1 < len(sys.argv):
            creds_path = sys.argv[i + 1]
        if arg == "--token" and i + 1 < len(sys.argv):
            token_path = sys.argv[i + 1]
    return creds_path, token_path


def main() -> int:
    creds_path, token_path = parse_args()

    if not os.path.exists(creds_path):
        print(f"ERROR: credentials.json not found at {creds_path}")
        print("Download it from Google Cloud Console > APIs & Services > Credentials")
        return 1

    if os.path.exists(token_path):
        print(f"Token already exists at {token_path}")
        resp = input("Overwrite? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return 0

    print("Starting OAuth2 flow...")
    print(f"Credentials: {creds_path}")
    print(f"Scopes: {SCOPES}")
    print()

    try:
        flow = build_flow(creds_path, SCOPES)
    except ImportError:
        print("ERROR: google-auth-oauthlib not installed")
        print(
            "Run: pip3 install google-api-python-client "
            "google-auth-httplib2 google-auth-oauthlib"
        )
        return 1

    creds = flow.run_local_server(port=8080, open_browser=False)
    print()
    print("If running on a remote server, tunnel port 8080 first:")
    print("  ssh -L 8080:localhost:8080 openclaw@VPS")
    print()

    save_credentials(creds, token_path, SCOPES)
    print(f"Token saved to {token_path}")
    print(f"Refresh token: {'present' if creds.refresh_token else 'MISSING'}")
    print()
    print("Done! The agent can now read Google Calendar + Gmail.")
    print("Auto-refresh handled by gcal-fetch / gcal-write via get_credentials().")
    return 0


if __name__ == "__main__":
    sys.exit(main())
