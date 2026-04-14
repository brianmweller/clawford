"""Tests for agents/shared/google_oauth.py.

Consolidates the near-identical gcal-auth.py scripts under
family-calendar and meetings-coach, plus the `get_credentials()`
load-and-refresh pattern duplicated in gcal-fetch.py / gcal-write.py /
workflowy-sync.py. Exposes:

  - build_flow(creds_path, scopes) -> InstalledAppFlow
  - save_credentials(creds, token_path, scopes) -> None
  - load_credentials(token_path, scopes) -> Credentials
  - get_credentials(creds_path, token_path, scopes) -> Credentials
  - refresh_if_stale(token_path, max_age_days) -> bool

Tests monkeypatch the Google modules so no real OAuth network I/O
happens, and use a minimal fake `Credentials` class for refresh paths.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.shared import google_oauth


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


# ─── Fakes ───────────────────────────────────────────────────────────


class FakeCredentials:
    """Minimal stand-in for google.oauth2.credentials.Credentials."""

    def __init__(
        self,
        *,
        token: str = "access-token",
        refresh_token: str = "refresh-token",
        token_uri: str = "https://oauth2.googleapis.com/token",
        client_id: str = "test-client-id",
        client_secret: str = "test-client-secret",
        scopes: list[str] | None = None,
        expired: bool = False,
        valid: bool = True,
    ) -> None:
        self.token = token
        self.refresh_token = refresh_token
        self.token_uri = token_uri
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or list(SCOPES)
        self.expired = expired
        self.valid = valid

    def refresh(self, _request) -> None:
        """Fake refresh: rotate the access token and clear expired."""
        self.token = "refreshed-access-token"
        self.expired = False
        self.valid = True


# ─── save_credentials / load_credentials round-trip ─────────────────


def test_save_credentials_writes_six_field_json(tmp_path: Path):
    token_path = tmp_path / "token.json"
    creds = FakeCredentials()
    google_oauth.save_credentials(creds, str(token_path), SCOPES)

    data = json.loads(token_path.read_text())
    assert data["token"] == "access-token"
    assert data["refresh_token"] == "refresh-token"
    assert data["token_uri"] == "https://oauth2.googleapis.com/token"
    assert data["client_id"] == "test-client-id"
    assert data["client_secret"] == "test-client-secret"
    assert data["scopes"] == SCOPES


def test_save_credentials_uses_creds_scopes_when_present(tmp_path: Path):
    token_path = tmp_path / "token.json"
    creds = FakeCredentials(scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    google_oauth.save_credentials(creds, str(token_path), SCOPES)
    data = json.loads(token_path.read_text())
    # Prefer the Credentials-object scopes over the caller-supplied fallback
    assert data["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_load_credentials_returns_credentials_with_fields(tmp_path: Path, monkeypatch):
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "t1",
        "refresh_token": "r1",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "csec",
        "scopes": SCOPES,
    }))

    captured = {}
    def fake_ctor(**kwargs):
        captured.update(kwargs)
        return FakeCredentials(**{
            k: v for k, v in kwargs.items()
            if k in {"token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes"}
        })

    monkeypatch.setattr(google_oauth, "_Credentials", fake_ctor)

    creds = google_oauth.load_credentials(str(token_path), SCOPES)
    assert captured["token"] == "t1"
    assert captured["refresh_token"] == "r1"
    assert captured["client_id"] == "cid"
    assert creds.refresh_token == "r1"


def test_load_credentials_raises_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        google_oauth.load_credentials(str(tmp_path / "nope.json"), SCOPES)


# ─── get_credentials — load → refresh if expired ────────────────────


def test_get_credentials_returns_valid_when_token_fresh(tmp_path: Path, monkeypatch):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")  # content ignored — monkeypatched ctor

    fresh = FakeCredentials(expired=False, valid=True)
    monkeypatch.setattr(google_oauth, "_Credentials", lambda **kwargs: fresh)

    creds = google_oauth.get_credentials(
        str(tmp_path / "credentials.json"),
        str(token_path),
        SCOPES,
    )
    assert creds is fresh
    assert creds.token == "access-token"


def test_get_credentials_refreshes_expired_token_and_rewrites_file(
    tmp_path: Path, monkeypatch
):
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "stale",
        "refresh_token": "r1",
        "token_uri": "u",
        "client_id": "c",
        "client_secret": "s",
        "scopes": SCOPES,
    }))

    expired = FakeCredentials(expired=True, valid=False)
    monkeypatch.setattr(google_oauth, "_Credentials", lambda **kwargs: expired)
    monkeypatch.setattr(google_oauth, "_Request", lambda: MagicMock())

    creds = google_oauth.get_credentials(
        str(tmp_path / "credentials.json"),
        str(token_path),
        SCOPES,
    )
    assert creds.token == "refreshed-access-token"
    # File was rewritten with the refreshed token
    rewritten = json.loads(token_path.read_text())
    assert rewritten["token"] == "refreshed-access-token"


def test_get_credentials_raises_when_token_missing_and_no_flow_available(
    tmp_path: Path, monkeypatch
):
    """When no token exists and no interactive flow is provided, raise.

    Headless production callers ensure token.json exists via SCP. An
    out-of-band interactive re-auth is a one-time action, not something
    get_credentials should implicitly trigger.
    """
    monkeypatch.setattr(google_oauth, "_Credentials", lambda **kwargs: FakeCredentials())
    with pytest.raises(FileNotFoundError):
        google_oauth.get_credentials(
            str(tmp_path / "credentials.json"),
            str(tmp_path / "missing-token.json"),
            SCOPES,
        )


# ─── build_flow ─────────────────────────────────────────────────────


def test_build_flow_wraps_installed_app_flow(tmp_path: Path, monkeypatch):
    creds_path = tmp_path / "credentials.json"
    creds_path.write_text(json.dumps({"installed": {}}))

    called = {}
    class FakeFlow:
        pass
    def fake_from_client_secrets(path, scopes):
        called["path"] = path
        called["scopes"] = scopes
        return FakeFlow()

    fake_cls = MagicMock()
    fake_cls.from_client_secrets_file = fake_from_client_secrets
    monkeypatch.setattr(google_oauth, "_InstalledAppFlow", fake_cls)

    flow = google_oauth.build_flow(str(creds_path), SCOPES)
    assert isinstance(flow, FakeFlow)
    assert called["path"] == str(creds_path)
    assert called["scopes"] == SCOPES


def test_build_flow_raises_when_creds_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        google_oauth.build_flow(str(tmp_path / "nope.json"), SCOPES)


# ─── refresh_if_stale ──────────────────────────────────────────────


def test_refresh_if_stale_returns_false_when_token_missing(tmp_path: Path):
    assert google_oauth.refresh_if_stale(
        str(tmp_path / "nope.json"), max_age_days=30, scopes=SCOPES
    ) is False


def test_refresh_if_stale_returns_false_when_token_fresh(tmp_path: Path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    # mtime is now — well under 30 days
    assert google_oauth.refresh_if_stale(
        str(token_path), max_age_days=30, scopes=SCOPES
    ) is False


def test_refresh_if_stale_refreshes_and_returns_true_when_stale(
    tmp_path: Path, monkeypatch
):
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "old",
        "refresh_token": "r1",
        "token_uri": "u",
        "client_id": "c",
        "client_secret": "s",
        "scopes": SCOPES,
    }))
    # Backdate mtime to 60 days ago
    stale_mtime = time.time() - (60 * 86400)
    os.utime(token_path, (stale_mtime, stale_mtime))

    stale = FakeCredentials(expired=True, valid=False)
    monkeypatch.setattr(google_oauth, "_Credentials", lambda **kwargs: stale)
    monkeypatch.setattr(google_oauth, "_Request", lambda: MagicMock())

    refreshed = google_oauth.refresh_if_stale(
        str(token_path), max_age_days=30, scopes=SCOPES
    )
    assert refreshed is True
    data = json.loads(token_path.read_text())
    assert data["token"] == "refreshed-access-token"


def test_refresh_if_stale_returns_false_on_refresh_failure(
    tmp_path: Path, monkeypatch
):
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "old",
        "refresh_token": "r1",
        "token_uri": "u",
        "client_id": "c",
        "client_secret": "s",
        "scopes": SCOPES,
    }))
    stale_mtime = time.time() - (60 * 86400)
    os.utime(token_path, (stale_mtime, stale_mtime))

    class BoomCreds(FakeCredentials):
        def refresh(self, _req):
            raise RuntimeError("network down")

    monkeypatch.setattr(
        google_oauth,
        "_Credentials",
        lambda **kwargs: BoomCreds(expired=True, valid=False),
    )
    monkeypatch.setattr(google_oauth, "_Request", lambda: MagicMock())

    assert google_oauth.refresh_if_stale(
        str(token_path), max_age_days=30, scopes=SCOPES
    ) is False
