#!/usr/bin/env python3
"""
gcal-auth.py — One-time OAuth2 setup for Google Calendar API.

Run this interactively (locally or via SSH tunnel) to authorize the agent
to read Sam's professional Google Calendar.

Usage: python3 gcal-auth.py [--credentials PATH] [--token PATH]

Defaults:
  --credentials: ~/.clawford/meetings-coach-workspace/credentials.json
  --token:       ~/.clawford/meetings-coach-workspace/token.json

Prerequisites:
  1. Google Cloud Console: create project (or reuse existing), enable Calendar API
  2. Create OAuth2 credentials (Desktop app type)
  3. Download credentials.json to the workspace
  4. Add Sam as a test user if the app is in "Testing" status

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
    "~/.clawford/meetings-coach-workspace/credentials.json"
)
DEFAULT_TOKEN = os.path.expanduser(
    "~/.clawford/meetings-coach-workspace/token.json"
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
    import json as _contract_json
    import sys as _contract_sys
    _contract_status = "ok"
    _contract_error = None
    try:
        _contract_rc = main()
        if _contract_rc not in (0, None):
            _contract_status = "error"
            _contract_error = f"main returned {_contract_rc}"
    except SystemExit as _contract_e:
        if _contract_e.code not in (0, None):
            _contract_status = "error"
            _contract_error = f"main exited with code {_contract_e.code}"
    except BaseException as _contract_e:  # noqa: BLE001
        _contract_status = "error"
        _contract_error = str(_contract_e)[:200]
    _contract_envelope = {"status": _contract_status}
    if _contract_error:
        _contract_envelope["error"] = _contract_error
    print(_contract_json.dumps(_contract_envelope))
    _contract_sys.exit(0)
