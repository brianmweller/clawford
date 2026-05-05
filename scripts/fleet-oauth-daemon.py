#!/usr/bin/env python3
"""fleet-oauth-daemon.py — VPS-side OAuth-callback listener.

Runs on the VPS, exposed only to the tailnet via `tailscale serve`
at `https://<your-tailscale-host>.<tailnet>.ts.net/`. Listens for two routes:

  GET /oauth/start?t=<HMAC>
      Verify the HMAC link (single-use, 10-min TTL), provision a state
      token, redirect the browser to Google's consent screen with the
      union scope set + the new Web-OAuth client_id.

  GET /oauth/callback?code=<code>&state=<state>
      Verify state matches a pending file, exchange the code with
      Google's token endpoint, fan out the resulting refresh token
      to every fleet workspace's token.json, send a Telegram
      confirmation, render a success page.

The HMAC gate keeps random tailnet members (in practice, just the operator's
own devices) from triggering consent flows by hitting /oauth/start
directly. The state token gates the callback against CSRF + replay.

Every Google round-trip is independent of the per-agent Desktop
OAuth client this fleet has used since bootstrap. The Web client
created in 2026-05-05 lives in the same Cloud project but registers
the .ts.net redirect URI; the Desktop client stays as a fallback.

Reads:
  ~/.clawford/fleet-oauth-tailnet-workspace/credentials.json (web client)
  ~/.clawford/fleet-oauth-tailnet-workspace/hmac-secret      (32 bytes hex)

Writes:
  ~/.clawford/<agent>-workspace/token.json (×4)
  ~/.clawford/fleet-oauth-tailnet-workspace/state/<state>.json (transient)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ─── Constants ───────────────────────────────────────────────────────

# Mirror scripts/reauth-fleet-token.py UNION_SCOPES. Duplicated rather
# than imported because reauth-fleet-token.py has a hyphenated name
# (importable only via importlib gymnastics) and the union list is
# stable enough that drift is a non-issue. Keep these in sync.
UNION_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/pubsub",
    "https://www.googleapis.com/auth/tasks",
]

FLEET_WORKSPACES = [
    "connector-workspace",
    "family-calendar-workspace",
    "meetings-coach-workspace",
    "shopping-workspace",
]

REDIRECT_URI = "https://<your-tailscale-host>.tail106e99.ts.net/oauth/callback"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

DAEMON_WORKSPACE = "~/.clawford/fleet-oauth-tailnet-workspace"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8765
STATE_TTL_S = 600          # 10 minutes from /oauth/start to /oauth/callback
LINK_TTL_S = 900           # 15 minutes from cron-issued link to first click


# ─── HMAC link tokens ────────────────────────────────────────────────


def make_signed_token(secret: bytes, expires_at: int) -> str:
    """Build a single-use bearer token for the /oauth/start gate.

    Format: base64url(payload).base64url(sig). Payload is JSON
    {expires_at, nonce}; sig is HMAC-SHA256 over the payload.
    """
    payload = json.dumps({
        "expires_at": int(expires_at),
        "nonce": secrets.token_urlsafe(16),
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    sig = hmac.new(secret, payload_b64.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{payload_b64}.{sig_b64}"


def verify_signed_token(secret: bytes, token: str, now: int) -> bool:
    """Constant-time HMAC verify + expiry check.

    Strips internal whitespace defensively — copy-paste through some
    chat clients / terminals soft-wraps long base64url payloads with
    literal spaces, which then get %20-encoded into the request URL.
    Base64url has no legitimate whitespace, so collapsing it is safe.
    """
    if not token:
        return False
    token = "".join(token.split())
    if "." not in token:
        return False
    payload_b64, sig_b64 = token.rsplit(".", 1)
    expected = hmac.new(secret, payload_b64.encode(), hashlib.sha256).digest()
    try:
        actual = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except Exception:
        return False
    if not hmac.compare_digest(expected, actual):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(
            payload_b64 + "=" * (-len(payload_b64) % 4)
        ))
    except Exception:
        return False
    return int(payload.get("expires_at", 0)) > int(now)


# ─── State file lifecycle ────────────────────────────────────────────


def create_state_file(state_dir: Path, ttl_s: int = STATE_TTL_S) -> str:
    """Mint a fresh state id, persist a marker, return the id."""
    state_dir.mkdir(parents=True, exist_ok=True)
    state_id = secrets.token_urlsafe(24)
    now = int(time.time())
    (state_dir / f"{state_id}.json").write_text(json.dumps({
        "created_at": now,
        "expires_at": now + ttl_s,
    }), encoding="utf-8")
    return state_id


def consume_state_file(state_dir: Path, state_id: str,
                       now: int | None = None) -> dict | None:
    """One-shot read+delete. Returns None on missing/expired (still cleans up)."""
    if not _safe_state_id(state_id):
        return None
    p = state_dir / f"{state_id}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        p.unlink(missing_ok=True)
        return None
    p.unlink(missing_ok=True)
    n = int(time.time()) if now is None else int(now)
    if int(data.get("expires_at", 0)) <= n:
        return None
    return data


def cleanup_state_dir(state_dir: Path, now: int | None = None) -> int:
    """Sweep expired state files. Returns count removed."""
    if not state_dir.exists():
        return 0
    n = int(time.time()) if now is None else int(now)
    removed = 0
    for p in state_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            p.unlink(missing_ok=True)
            removed += 1
            continue
        if int(data.get("expires_at", 0)) <= n:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def _safe_state_id(s: str) -> bool:
    return bool(s) and len(s) <= 128 and all(
        c.isalnum() or c in "-_" for c in s
    )


# ─── Google consent URL ──────────────────────────────────────────────


def build_google_auth_url(client_id: str, redirect_uri: str,
                          scopes: list[str], state: str) -> str:
    """Compose the URL we 302 the phone browser to."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        # offline + consent → guaranteed refresh_token in the token response,
        # even on re-consent for the same client+account.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


# ─── Token exchange + fan-out ────────────────────────────────────────


def _exchange_code_for_token(*, client_id: str, client_secret: str,
                             redirect_uri: str, code: str) -> dict:
    """POST to Google's token endpoint. Returns the parsed JSON body."""
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fan_out_token(google_response: dict, *, client_id: str,
                  client_secret: str, scopes: list[str]) -> list[Path]:
    """Write the same token.json to every fleet workspace.

    Raises ValueError if the response is missing a refresh_token —
    without it the new tokens die at the next access-token expiry,
    which is worse than failing loud now.
    """
    if not google_response.get("refresh_token"):
        raise ValueError(
            "Google response missing refresh_token "
            "(prompt=consent + access_type=offline should guarantee one)"
        )

    effective_scopes = (
        google_response.get("scope", "").split() or list(scopes)
    )
    token_payload = {
        "token": google_response["access_token"],
        "refresh_token": google_response["refresh_token"],
        "token_uri": GOOGLE_TOKEN_URL,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": effective_scopes,
    }
    written: list[Path] = []
    for ws in FLEET_WORKSPACES:
        path = Path(os.path.expanduser(f"~/.clawford/{ws}/token.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(token_payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        written.append(path)
    return written


# ─── Telegram confirmation ───────────────────────────────────────────


def _send_telegram_confirmation(text: str) -> bool:
    """Best-effort post-success ping on the fix-it bot. Never raises."""
    try:
        from agents.shared import telegram_api  # type: ignore
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return False
        return telegram_api.send_message(token, chat_id, text)
    except Exception:
        return False


# ─── Router (testable seam between BaseHTTPRequestHandler and logic) ─


class Router:
    """Pure routing logic. No socket, no handler base class.

    Tests instantiate this with a config dict and call .handle(path, query)
    directly. The HTTPServer wrapper below translates real requests into
    the same call shape.
    """

    def __init__(self, config: dict):
        self.config = config

    def handle(self, path: str, query: dict) -> tuple[int, list[tuple[str, str]], bytes]:
        if path == "/oauth/start":
            return self._start(query)
        if path == "/oauth/callback":
            return self._callback(query)
        if path == "/healthz":
            return 200, [("Content-Type", "text/plain")], b"ok\n"
        return 404, [("Content-Type", "text/plain")], b"not found\n"

    def _start(self, query: dict):
        token = query.get("t")
        if not token:
            return 400, [("Content-Type", "text/plain")], b"missing t param\n"
        if not verify_signed_token(self.config["hmac_secret"], token,
                                   now=int(time.time())):
            return 403, [("Content-Type", "text/plain")], b"invalid or expired link\n"
        state_id = create_state_file(self.config["state_dir"])
        url = build_google_auth_url(
            client_id=self.config["client_id"],
            redirect_uri=self.config["redirect_uri"],
            scopes=self.config["scopes"],
            state=state_id,
        )
        return 302, [("Location", url)], b""

    def _callback(self, query: dict):
        code = query.get("code")
        state = query.get("state")
        if not code or not state:
            return 400, [("Content-Type", "text/plain")], b"missing code or state\n"
        consumed = consume_state_file(self.config["state_dir"], state)
        if consumed is None:
            return 403, [("Content-Type", "text/plain")], b"unknown or expired state\n"

        try:
            response = _exchange_code_for_token(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"],
                code=code,
            )
        except Exception as e:  # noqa: BLE001
            return 500, [("Content-Type", "text/plain")], (
                f"token exchange failed: {type(e).__name__}: {e}\n".encode()
            )

        try:
            written = fan_out_token(
                response,
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                scopes=self.config["scopes"],
            )
        except ValueError as e:
            return 400, [("Content-Type", "text/plain")], f"{e}\n".encode()

        _send_telegram_confirmation(
            f"✅ Fleet OAuth refreshed across {len(written)} workspaces."
        )
        body = (
            "<html><body style='font-family:sans-serif;padding:2em'>"
            f"<h1>✅ Fleet OAuth refreshed</h1>"
            f"<p>{len(written)} workspaces updated. Safe to close this tab.</p>"
            "</body></html>"
        ).encode("utf-8")
        return 200, [("Content-Type", "text/html; charset=utf-8")], body


# Test alias — keep the daemon name semantic in production code
# while letting tests build a harness without needing a server.
TestHarness = Router


# ─── HTTP listener ───────────────────────────────────────────────────


def _build_handler_class(router: Router):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib API
            parsed = urllib.parse.urlparse(self.path)
            query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            try:
                status, headers, body = router.handle(parsed.path, query)
            except Exception as e:  # noqa: BLE001
                self.send_error(500, f"{type(e).__name__}: {e}")
                return
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 — stdlib API
            sys.stderr.write(
                f"[{self.log_date_time_string()}] {self.address_string()} "
                f"{format % args}\n"
            )
    return _Handler


def _load_config() -> dict:
    """Read credentials.json + hmac-secret from the daemon workspace."""
    workspace = Path(os.path.expanduser(DAEMON_WORKSPACE))
    creds_path = workspace / "credentials.json"
    secret_path = workspace / "hmac-secret"
    state_dir = workspace / "state"

    creds_raw = json.loads(creds_path.read_text(encoding="utf-8"))
    web = creds_raw.get("web") or creds_raw.get("installed") or {}
    client_id = web["client_id"]
    client_secret = web["client_secret"]

    if not secret_path.exists():
        secret_path.write_bytes(secrets.token_hex(32).encode())
        os.chmod(secret_path, 0o600)
    secret = secret_path.read_text(encoding="utf-8").strip().encode()

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "scopes": UNION_SCOPES,
        "state_dir": state_dir,
        "hmac_secret": secret,
        "home": Path(os.path.expanduser("~")),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fleet OAuth callback daemon.")
    ap.add_argument("--host", default=DEFAULT_LISTEN_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    ap.add_argument("--print-link", action="store_true",
                    help="Generate a one-time HMAC-signed /oauth/start URL and exit.")
    args = ap.parse_args(argv)

    config = _load_config()

    if args.print_link:
        token = make_signed_token(
            config["hmac_secret"],
            expires_at=int(time.time()) + LINK_TTL_S,
        )
        host = "<your-tailscale-host>.tail106e99.ts.net"
        print(f"https://{host}/oauth/start?t={token}")
        return 0

    cleanup_state_dir(config["state_dir"])
    router = Router(config)
    server = HTTPServer((args.host, args.port), _build_handler_class(router))
    print(f"[fleet-oauth-daemon] listening on http://{args.host}:{args.port}")
    print(f"[fleet-oauth-daemon] redirect_uri={config['redirect_uri']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[fleet-oauth-daemon] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
