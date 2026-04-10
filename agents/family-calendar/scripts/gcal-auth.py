#!/usr/bin/env python3
"""
gcal-auth.py — One-time OAuth2 setup for Google Calendar API.

Run this interactively on the VPS (via SSH) to authorize the agent
to read family Google Calendars.

Usage: python3 gcal-auth.py [--credentials PATH] [--token PATH]

Defaults:
  --credentials: ~/.openclaw/family-calendar-workspace/credentials.json
  --token:       ~/.openclaw/family-calendar-workspace/token.json

Prerequisites:
  1. Google Cloud Console: create project, enable Calendar API
  2. Create OAuth2 credentials (Desktop app type)
  3. Download credentials.json to the workspace

After running, token.json will contain a refresh token that auto-renews.
Re-run this script only if the token is revoked or the Google Cloud project changes.
"""

import json
import os
import sys

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


def parse_args():
    creds_path = DEFAULT_CREDS
    token_path = DEFAULT_TOKEN
    for i, arg in enumerate(sys.argv):
        if arg == "--credentials" and i + 1 < len(sys.argv):
            creds_path = sys.argv[i + 1]
        if arg == "--token" and i + 1 < len(sys.argv):
            token_path = sys.argv[i + 1]
    return creds_path, token_path


def main():
    creds_path, token_path = parse_args()

    if not os.path.exists(creds_path):
        print(f"ERROR: credentials.json not found at {creds_path}")
        print("Download it from Google Cloud Console > APIs & Services > Credentials")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib not installed")
        print("Run: pip3 install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        sys.exit(1)

    # Check if token already exists
    if os.path.exists(token_path):
        print(f"Token already exists at {token_path}")
        resp = input("Overwrite? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            sys.exit(0)

    print(f"Starting OAuth2 flow...")
    print(f"Credentials: {creds_path}")
    print(f"Scopes: {SCOPES}")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)

    # Try local server first (works if port 8080 is available)
    # Falls back to console-based flow for headless servers
    try:
        creds = flow.run_local_server(port=8080, open_browser=False)
        print()
        print("Open this URL in your browser to authorize:")
        print(f"  http://localhost:8080")
        print()
        print("If running on a remote server, use SSH tunnel:")
        print(f"  ssh -L 8080:localhost:8080 openclaw@VPS")
    except Exception:
        print("Local server not available, using console flow...")
        creds = flow.run_console()

    # Save token
    with open(token_path, "w") as f:
        json.dump({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        }, f, indent=2)

    print()
    print(f"Token saved to {token_path}")
    print(f"Refresh token: {'present' if creds.refresh_token else 'MISSING'}")
    print()
    print("Done! The agent can now read Google Calendar.")
    print("This token auto-refreshes — you should not need to run this again")
    print("unless the Google Cloud project is deleted or consent is revoked.")


if __name__ == "__main__":
    main()
