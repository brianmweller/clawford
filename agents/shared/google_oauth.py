"""google_oauth — shared Google OAuth2 helpers for fleet agents.

Consolidates the duplication across:
  - agents/family-calendar/scripts/gcal-auth.py (interactive flow)
  - agents/meetings-coach/scripts/gcal-auth.py  (same, different workspace)
  - agents/family-calendar/scripts/gcal-fetch.py get_credentials()
  - agents/family-calendar/scripts/gcal-write.py get_credentials()
  - agents/meetings-coach/scripts/workflowy-sync.py token loader

Four layers:

  build_flow(creds_path, scopes) -> InstalledAppFlow
      Thin wrapper around google_auth_oauthlib. Use it to run an
      interactive authorization flow (run_local_server / run_console).

  load_credentials(token_path, scopes) -> Credentials
      Pure token loader. Reads the six-field token.json written by
      save_credentials and constructs a google.oauth2.Credentials.
      Does NOT refresh. Raises FileNotFoundError if no token.

  save_credentials(creds, token_path, scopes) -> None
      Writes the six-field dict atomically. Prefers creds.scopes when
      present, falls back to the caller-supplied scopes.

  get_credentials(creds_path, token_path, scopes) -> Credentials
      Full load + refresh-if-expired pipeline for runtime use from
      gcal-fetch.py / gcal-write.py / etc. Reads token.json, builds a
      Credentials, and calls creds.refresh(Request()) if expired.
      Rewrites token.json after a successful refresh so the rotated
      access-token persists. Raises FileNotFoundError if no token.

  refresh_if_stale(token_path, max_age_days, scopes) -> bool
      mtime-gated preemptive refresh. Returns True if the token was
      rewritten, False if fresh or missing or if refresh failed.
      Used by long-running daemons that want to keep the refresh_token
      warm ahead of an expiry.

Google OAuth deployment pattern (see memory: feedback_google_oauth.md):
Run the interactive flow locally, SCP token.json to the VPS, add the
Google account as a test user on the Cloud project before first auth,
use Desktop-type OAuth client credentials.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any


# Known OAuth scope constants. Callers bundle these as needed — the
# library doesn't enforce any particular combination. Each agent's
# auth script (e.g. gmail-auth.py, gcal-auth.py) is the single source
# of truth for which scopes that agent actually needs.
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
# 2026-04-20 — added for Huckle's real-time Gmail triage (pull from
# the huckle-gmail-pull subscription). Gmail users.watch() itself does
# NOT require this scope — only the consumer side does.
PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"


# Google packages imported lazily at module load so tests can
# monkeypatch the module-level references. Each reference resolves to
# the real class at import time on machines where google-auth is
# installed; tests replace these with fakes before calling into the
# module.
try:
    from google.auth.transport.requests import Request as _Request  # type: ignore
    from google.oauth2.credentials import Credentials as _Credentials  # type: ignore
    from google_auth_oauthlib.flow import InstalledAppFlow as _InstalledAppFlow  # type: ignore
except ImportError:  # pragma: no cover — exercised only on skinny envs
    _Request = None  # type: ignore
    _Credentials = None  # type: ignore
    _InstalledAppFlow = None  # type: ignore


def build_flow(creds_path: str, scopes: list[str]) -> Any:
    """Return an InstalledAppFlow for the given credentials file.

    Raises FileNotFoundError if creds_path doesn't exist so the caller
    can report a clean error instead of failing deep inside the google
    library.
    """
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"credentials.json not found at {creds_path}")
    return _InstalledAppFlow.from_client_secrets_file(creds_path, scopes)


def save_credentials(creds: Any, token_path: str, scopes: list[str]) -> None:
    """Write the six-field token.json dict atomically."""
    # Prefer the Credentials-object scopes when present — they reflect
    # what was actually granted during the flow, which can differ from
    # what the caller requested.
    effective_scopes = list(creds.scopes) if getattr(creds, "scopes", None) else list(scopes)
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": effective_scopes,
    }
    os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
    tmp = token_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, token_path)


def load_credentials(token_path: str, scopes: list[str]) -> Any:
    """Read token.json and construct a Credentials. No refresh."""
    if not os.path.exists(token_path):
        raise FileNotFoundError(f"token.json not found at {token_path}")
    with open(token_path, encoding="utf-8") as f:
        token_data = json.load(f)
    return _Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes") or list(scopes),
    )


def get_credentials(creds_path: str, token_path: str, scopes: list[str]) -> Any:
    """Load token.json, refresh if expired, return a valid Credentials.

    Raises FileNotFoundError if token.json doesn't exist — headless
    callers should SCP an existing token into place, not trigger an
    interactive flow implicitly.

    The creds_path parameter is accepted so the caller can surface a
    hint (re-run gcal-auth.py at PATH) on refresh failure, but it is
    not consulted on the happy path.
    """
    creds = load_credentials(token_path, scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(_Request())
        save_credentials(creds, token_path, scopes)
    return creds


def refresh_if_stale(
    token_path: str,
    max_age_days: int = 30,
    scopes: list[str] | None = None,
) -> bool:
    """If token.json is older than max_age_days, force a refresh.

    Returns True iff the token was successfully refreshed and rewritten.
    Returns False if the token is fresh, missing, or if refresh failed.
    Never raises — a stale-refresh is opportunistic housekeeping, and
    the caller should be able to ignore the result.
    """
    if not os.path.exists(token_path):
        return False
    try:
        age_days = (time.time() - os.path.getmtime(token_path)) / 86400
    except OSError:
        return False
    if age_days <= max_age_days:
        return False

    effective_scopes = scopes or []
    try:
        creds = load_credentials(token_path, effective_scopes)
        if not creds.refresh_token:
            return False
        creds.refresh(_Request())
        save_credentials(creds, token_path, effective_scopes)
        return True
    except Exception:
        return False
