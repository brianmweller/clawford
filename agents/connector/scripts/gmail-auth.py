#!/usr/bin/env python3
"""gmail-auth.py — interactive OAuth flow for Huckle Cat.

Re-run when the token scope list changes (e.g. adding pubsub for the
real-time push listener) or when the token is revoked.

Standard fleet pattern: run this locally on the operator's workstation,
authorize in the browser, SCP the resulting token.json to the VPS.
The VPS itself has no public port / no browser, so it can't run the
flow — see memory: reference_google_oauth.md.

Scopes:
  gmail.readonly   — list/threads/history/getProfile
  gmail.compose    — drafts.create (NEVER drafts.send, code-enforced)
  calendar.readonly — for availability.py free-slot calculations
  pubsub           — pull from the huckle-gmail-pull subscription
                     (added 2026-04-20 for the push listener)

Usage:
  python3 gmail-auth.py [--credentials PATH] [--token PATH]

Defaults:
  --credentials: ~/.clawford/connector-workspace/credentials.json
  --token:       ~/.clawford/connector-workspace/token.json

Prerequisites:
  1. Google Cloud Console: project with Gmail API + Calendar API +
     Pub/Sub API enabled.
  2. OAuth consent screen: add the operator's Google account as a test user.
  3. Desktop-app OAuth credentials downloaded to --credentials path.
  4. Pub/Sub topic + pull subscription created (see
     DEPLOY.md for the one-time checklist).

After a successful run, SCP the token to the VPS:
  scp ~/.clawford/connector-workspace/token.json \\
      openclaw@<your-tailscale-host>:~/.clawford/connector-workspace/token.json
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

from agents.shared.google_oauth import build_flow, save_credentials  # noqa: E402


SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/pubsub",
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
        print("Download it from Google Cloud Console > APIs & Services > Credentials")
        return 1

    if os.path.exists(token_path):
        print(f"Token already exists at {token_path}")
        print("A fresh run overwrites the token and invalidates the VPS copy")
        print("until you SCP the new one over.")
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

    creds = flow.run_local_server(port=8765, open_browser=True)
    print()

    save_credentials(creds, token_path, SCOPES)
    print(f"Token saved to {token_path}")
    print(f"Refresh token: {'present' if creds.refresh_token else 'MISSING'}")
    print()
    print("Next step — SCP to the VPS:")
    print(f"  scp {token_path} \\")
    print("      openclaw@<your-tailscale-host>:~/.clawford/connector-workspace/token.json")
    print()
    print("Then verify on the VPS:")
    print("  ssh openclaw@<your-tailscale-host> 'python3 ~/.clawford/connector-workspace/"
          "scripts/gmail-watch-renew.py'")
    return 0


if __name__ == "__main__":
    import json as _contract_json
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
    sys.exit(0)
