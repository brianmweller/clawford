#!/usr/bin/env python3
"""
gcal-auth.py — One-time OAuth2 setup for the connector's Google access.

Connector's daily-refresh.py reads Gmail headers + GCal events to update
last_interaction signals in the shared brain. It previously piggy-backed
on ~/.clawford/family-calendar-workspace/token.json, which silently
broke under bubblewrap workspace isolation (the connector process can't
see another workspace's files). Giving connector its own token removes
the cross-workspace coupling.

Usage: python3 gcal-auth.py [--credentials PATH] [--token PATH]

Defaults:
  --credentials: ~/.clawford/connector-workspace/credentials.json
  --token:       ~/.clawford/connector-workspace/token.json

Prerequisites:
  1. Copy credentials.json from family-calendar-workspace to
     connector-workspace (same Desktop OAuth client is fine; no need to
     provision a new Google Cloud Console project).
  2. Run locally; tunnel port 8080 if running on the VPS.
  3. SCP the resulting token.json to
     openclaw@VPS:~/.clawford/connector-workspace/token.json

Scopes are read-only — daily-refresh never writes calendar/email.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.google_oauth import build_flow, save_credentials

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

DEFAULT_CREDS = os.path.expanduser(
    "~/.clawford/connector-workspace/credentials.json"
)
DEFAULT_TOKEN = os.path.expanduser(
    "~/.clawford/connector-workspace/token.json"
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
        print(
            "Copy it from ~/.clawford/family-calendar-workspace/credentials.json "
            "(same Desktop OAuth client is fine) or download a new one from "
            "Google Cloud Console > APIs & Services > Credentials."
        )
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
    print("Done! Connector's daily-refresh can now read Gmail + GCal.")
    print("SCP this token to openclaw@VPS:~/.clawford/connector-workspace/token.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
