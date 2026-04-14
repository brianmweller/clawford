"""Tests for agents/shared/playwright_profile.py.

Covers the parts of the persistent-Chromium-profile bootstrap that can
be unit-tested without actually launching a browser: profile-dir
resolution, singleton-lock cleanup, xvfb process management (via
subprocess monkeypatching), and the launch_persistent_profile shim
(via a fake sync_playwright factory).

Real browser launches belong in manual smoke tests against
linkedin-auth.py / linkedin-keepalive.py — anything that actually
spawns Chromium resists stubbing.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.shared import playwright_profile


# ─── cleanup_profile_lock ───────────────────────────────────────────


def test_cleanup_profile_lock_removes_known_singleton_files(tmp_path: Path):
    profile = tmp_path / "linkedin-profile"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / name).write_text("stale")

    removed = playwright_profile.cleanup_profile_lock(profile)

    assert sorted(removed) == ["SingletonCookie", "SingletonLock", "SingletonSocket"]
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert not (profile / name).exists()


def test_cleanup_profile_lock_tolerates_missing_files(tmp_path: Path):
    profile = tmp_path / "empty-profile"
    profile.mkdir()
    removed = playwright_profile.cleanup_profile_lock(profile)
    assert removed == []


def test_cleanup_profile_lock_tolerates_missing_profile_dir(tmp_path: Path):
    """A profile dir that doesn't exist yet should not crash — the
    cleanup step is a best-effort precondition, not an assertion."""
    removed = playwright_profile.cleanup_profile_lock(tmp_path / "nope")
    assert removed == []


def test_cleanup_profile_lock_preserves_non_lock_files(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("x")
    (profile / "Cookies").write_text("real-cookies")
    (profile / "Preferences").write_text("real-prefs")

    playwright_profile.cleanup_profile_lock(profile)

    assert (profile / "Cookies").exists()
    assert (profile / "Preferences").exists()
    assert not (profile / "SingletonLock").exists()


# ─── ensure_profile_dir ─────────────────────────────────────────────


def test_ensure_profile_dir_creates_missing_dir(tmp_path: Path):
    profile = tmp_path / "news-digest" / "linkedin-profile"
    assert not profile.exists()
    playwright_profile.ensure_profile_dir(profile)
    assert profile.is_dir()


def test_ensure_profile_dir_is_idempotent(tmp_path: Path):
    profile = tmp_path / "profile"
    playwright_profile.ensure_profile_dir(profile)
    playwright_profile.ensure_profile_dir(profile)  # second call — no raise
    assert profile.is_dir()


# ─── ensure_xvfb ────────────────────────────────────────────────────


def test_ensure_xvfb_starts_new_process_and_sets_display(monkeypatch, tmp_path):
    """ensure_xvfb should start Xvfb on the requested display and set
    the DISPLAY env var. No existing Xvfb on :99, so it launches."""
    started = {}

    def fake_popen(argv, **kwargs):
        started["argv"] = argv
        started["kwargs"] = kwargs
        m = MagicMock()
        m.poll.return_value = None  # still running
        return m

    # No pre-existing Xvfb found
    monkeypatch.setattr(playwright_profile, "_is_xvfb_running", lambda n: False)
    monkeypatch.setattr(playwright_profile.subprocess, "Popen", fake_popen)
    monkeypatch.delenv("DISPLAY", raising=False)

    proc = playwright_profile.ensure_xvfb(display_num=99)

    assert proc is not None
    assert started["argv"][0] == "Xvfb"
    assert ":99" in started["argv"]
    assert os.environ.get("DISPLAY") == ":99"


def test_ensure_xvfb_noop_when_already_running(monkeypatch):
    """If Xvfb is already running on the requested display, just set
    DISPLAY and return None (no new process)."""
    monkeypatch.setattr(playwright_profile, "_is_xvfb_running", lambda n: True)

    def fail_popen(*a, **kw):
        raise AssertionError("Popen should not be called when Xvfb is already up")
    monkeypatch.setattr(playwright_profile.subprocess, "Popen", fail_popen)
    monkeypatch.delenv("DISPLAY", raising=False)

    proc = playwright_profile.ensure_xvfb(display_num=99)
    assert proc is None
    assert os.environ.get("DISPLAY") == ":99"


def test_ensure_xvfb_uses_custom_screen_size(monkeypatch):
    started = {}
    def fake_popen(argv, **kwargs):
        started["argv"] = argv
        return MagicMock(poll=MagicMock(return_value=None))

    monkeypatch.setattr(playwright_profile, "_is_xvfb_running", lambda n: False)
    monkeypatch.setattr(playwright_profile.subprocess, "Popen", fake_popen)

    playwright_profile.ensure_xvfb(display_num=99, screen_size="1920x1080x24")

    assert "1920x1080x24" in started["argv"]


# ─── launch_persistent_profile ─────────────────────────────────────


def test_launch_persistent_profile_calls_playwright_with_expected_args(
    monkeypatch, tmp_path
):
    profile = tmp_path / "linkedin-profile"
    profile.mkdir()

    captured = {}

    class FakeBrowser:
        pass

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            captured["kwargs"] = kwargs
            return FakeBrowser()

    class FakePW:
        chromium = FakeChromium()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_sync_playwright():
        return FakePW()

    monkeypatch.setattr(playwright_profile, "_sync_playwright", fake_sync_playwright)

    # Use a context-manager style so the caller can close cleanly
    with playwright_profile.launch_persistent_profile(
        str(profile),
        headless=True,
    ) as browser:
        assert isinstance(browser, FakeBrowser)

    kwargs = captured["kwargs"]
    assert kwargs["user_data_dir"] == str(profile)
    assert kwargs["headless"] is True
    assert "--no-sandbox" in kwargs["args"]
    assert "--disable-gpu" in kwargs["args"]


def test_launch_persistent_profile_cleans_locks_before_launch(
    monkeypatch, tmp_path
):
    """Stale SingletonLock files are auto-removed before launch —
    otherwise Chromium refuses to start in the existing profile."""
    profile = tmp_path / "linkedin-profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("stale")

    class FakePW:
        class chromium:
            @staticmethod
            def launch_persistent_context(**kwargs):
                # At launch time, SingletonLock should already be gone
                assert not (profile / "SingletonLock").exists()
                return MagicMock()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr(playwright_profile, "_sync_playwright", lambda: FakePW())

    with playwright_profile.launch_persistent_profile(profile) as _:
        pass


def test_launch_persistent_profile_uses_xvfb_display_when_requested(
    monkeypatch, tmp_path
):
    """Passing xvfb_display should call ensure_xvfb."""
    profile = tmp_path / "profile"
    profile.mkdir()

    calls = []
    monkeypatch.setattr(
        playwright_profile,
        "ensure_xvfb",
        lambda display_num=99, **kw: calls.append(display_num),
    )

    class FakePW:
        class chromium:
            @staticmethod
            def launch_persistent_context(**kwargs):
                return MagicMock()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr(playwright_profile, "_sync_playwright", lambda: FakePW())

    with playwright_profile.launch_persistent_profile(profile, xvfb_display=99) as _:
        pass

    assert calls == [99]
