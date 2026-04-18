#!/usr/bin/env python3
"""
krisp-auth-manual.py — Manual Krisp OAuth flow without the MCP SDK.

Uses the existing client_info.json (client_id + client_secret), performs
the OAuth 2.1 authorization code flow with PKCE, and saves fresh tokens.

Run this LOCALLY (not in Docker) — it opens a browser and listens on
http://localhost:19823/callback.

Usage:
  python3 krisp-auth-manual.py

Token storage — cross-platform `~/.clawford/meetings-coach-workspace/
cache/krisp-tokens/`. This mirrors the VPS workspace layout exactly so
the scp-to-VPS step is trivial (identical paths on both sides). The
`~/.clawford/` tree is outside the git repo, so tokens never risk
accidental commit. The OLD hardcoded E:/Dropbox/Startup/Flux/data/
path is no longer used — it coupled Clawford to the Flux project.
"""

import base64
import hashlib
import json
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import httpx

TOKEN_DIR = Path.home() / ".clawford" / "meetings-coach-workspace" / "cache" / "krisp-tokens"
AUTH_ENDPOINT = "https://api.krisp.ai/platform/v1/oauth2/authorize"
TOKEN_ENDPOINT = "https://api.krisp.ai/platform/v1/oauth2/token"
REDIRECT_URI = "http://localhost:19823/callback"
CALLBACK_PORT = 19823

SCOPES = " ".join([
    "user::me::read",
    "user::meetings:metadata::read",
    "user::meetings:notes::read",
    "user::meetings:transcripts::read",
    "user::meetings::list",
    "user::activities::list",
])


class CallbackHandler(BaseHTTPRequestHandler):
    captured_code = None
    captured_state = None
    captured_error = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        CallbackHandler.captured_code = params.get("code", [None])[0]
        CallbackHandler.captured_state = params.get("state", [None])[0]
        CallbackHandler.captured_error = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

        if CallbackHandler.captured_error:
            msg = f"<h2>Error: {CallbackHandler.captured_error}</h2>"
        else:
            msg = "<h2>Success! You can close this tab.</h2>"

        self.wfile.write(f"<html><body>{msg}</body></html>".encode())

    def log_message(self, *args, **kwargs):
        pass  # Silent


def pkce_pair():
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def main():
    # Ensure the token dir exists (cross-platform, no-op if already there)
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    # Load client_info
    client_info_path = TOKEN_DIR / "client_info.json"
    if not client_info_path.exists():
        print(f"ERROR: {client_info_path} not found", file=sys.stderr)
        print(
            f"HINT: copy client_info.json to {TOKEN_DIR} — this file holds "
            f"the Krisp OAuth client_id + client_secret, which are long-lived "
            f"credentials registered once with Krisp.",
            file=sys.stderr,
        )
        sys.exit(1)

    client_info = json.loads(client_info_path.read_text())
    client_id = client_info.get("client_id")
    client_secret = client_info.get("client_secret")

    if not client_id or not client_secret:
        print("ERROR: client_id or client_secret missing in client_info.json", file=sys.stderr)
        sys.exit(1)

    # Generate PKCE and state
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)

    # Build authorization URL
    auth_params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(auth_params)}"

    print("Krisp Manual OAuth Flow")
    print("=" * 60)
    print()
    print("Opening browser to authorize...")
    print(f"URL: {auth_url}")
    print()

    # Start local callback server
    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), CallbackHandler)
    server.timeout = 300

    # Open browser
    webbrowser.open(auth_url)

    print(f"Waiting for callback on http://localhost:{CALLBACK_PORT}/callback...")
    print("(timeout: 5 minutes)")

    # Handle one request (the callback)
    server.handle_request()
    server.server_close()

    if CallbackHandler.captured_error:
        print(f"ERROR: {CallbackHandler.captured_error}", file=sys.stderr)
        sys.exit(1)

    if not CallbackHandler.captured_code:
        print("ERROR: No code received from callback", file=sys.stderr)
        sys.exit(1)

    if CallbackHandler.captured_state != state:
        print(f"ERROR: state mismatch (got {CallbackHandler.captured_state})", file=sys.stderr)
        sys.exit(1)

    code = CallbackHandler.captured_code
    print(f"Received authorization code: {code[:20]}...")
    print()

    # Exchange code for tokens
    print("Exchanging code for tokens...")
    resp = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        auth=(client_id, client_secret),
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"ERROR: Token exchange failed: {resp.status_code}")
        print(resp.text)
        sys.exit(1)

    tokens = resp.json()
    access_preview = tokens.get("access_token", "")[:30]
    print(f"Access token: {access_preview}...")
    print(f"Scope: {tokens.get('scope', '?')}")
    print(f"Expires in: {tokens.get('expires_in', '?')}s")
    print()

    # Save tokens
    tokens_path = TOKEN_DIR / "tokens.json"
    tokens_path.write_text(json.dumps(tokens, indent=2))
    print(f"Saved to: {tokens_path}")
    print()

    print("Done! Now copy tokens.json to the VPS:")
    print(f"  scp -i ~/.ssh/id_ed25519 {tokens_path} \\")
    print(f"    openclaw@198.51.100.42:/home/openclaw/.clawford/meetings-coach-workspace/cache/krisp-tokens/")


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
