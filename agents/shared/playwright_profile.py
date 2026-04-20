"""playwright_profile — persistent Chromium profile launcher.

Consolidates the scaffolding around persistent-profile Chromium used
by the Google Messages Web scraper and any future stock-Playwright
consumer that needs cookies-live / tokens-refresh semantics. Exposes:

  cleanup_profile_lock(profile_dir) -> list[str]
      Remove Chromium's SingletonLock / SingletonCookie / SingletonSocket
      files if present. Chromium refuses to start in a profile that
      still has stale locks from a crashed prior run.

  ensure_profile_dir(profile_dir) -> None
      Create the profile directory (parents included) if missing.

  ensure_xvfb(display_num=99, *, screen_size="1280x1024x24") -> Popen | None
      Start Xvfb on :<display_num> if not already running and set the
      DISPLAY env var. Returns the Popen on a fresh launch, None if
      Xvfb was already up. Caller is responsible for terminating the
      Popen in its own cleanup path.

  launch_persistent_profile(profile_dir, *, headless=False,
                            xvfb_display=None, executable_path=None,
                            args=None) -> context manager
      Launch a Playwright persistent Chromium context. Cleans locks
      first, optionally starts Xvfb. Yields the browser context.

The full auth bootstrap in linkedin-auth.py uses Chromium via raw
subprocess (not Playwright) because CAPTCHAs need a real X session +
socat + remote debugging port — this module's launch helper doesn't
cover that path, but the cleanup/xvfb helpers do.

**Gotcha (memory: feedback_playwright_threading.md):** in sync
Playwright, `time.sleep()` blocks route handlers — use
`page.wait_for_timeout()` in any polling loop that needs route
interception to fire. This module doesn't set up route handlers but
consumers should know.
"""
from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_DEFAULT_ARGS = ("--no-sandbox", "--disable-gpu")


# Indirection so tests can monkeypatch without importing playwright.
def _sync_playwright():
    from playwright.sync_api import sync_playwright  # type: ignore
    return sync_playwright()


def cleanup_profile_lock(profile_dir: str | Path) -> list[str]:
    """Remove stale Chromium singleton lock files. Returns the names
    of files actually removed (for logging).
    """
    profile = Path(profile_dir)
    if not profile.is_dir():
        return []
    removed: list[str] = []
    for name in _LOCK_FILES:
        path = profile / name
        if path.exists():
            try:
                path.unlink()
                removed.append(name)
            except OSError:
                pass
    return removed


def ensure_profile_dir(profile_dir: str | Path) -> None:
    """Create the profile directory if missing."""
    Path(profile_dir).mkdir(parents=True, exist_ok=True)


def _is_xvfb_running(display_num: int) -> bool:
    """Probe whether an Xvfb process is already bound to :<display_num>.

    Best-effort: looks for `Xvfb :<N>` in `pgrep -af`. Returns False on
    any pgrep failure so callers err on the side of launching a fresh
    Xvfb.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-af", f"Xvfb :{display_num}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ensure_xvfb(
    display_num: int = 99,
    *,
    screen_size: str = "1280x1024x24",
) -> subprocess.Popen | None:
    """Start Xvfb on :<display_num> if not already running. Sets the
    DISPLAY env var to ':<display_num>' so subsequent Chromium launches
    attach to the virtual display.

    Returns the Popen on a fresh launch (caller should terminate it in
    its cleanup path), or None if Xvfb was already up.
    """
    os.environ["DISPLAY"] = f":{display_num}"
    if _is_xvfb_running(display_num):
        return None
    proc = subprocess.Popen(
        [
            "Xvfb",
            f":{display_num}",
            "-screen",
            "0",
            screen_size,
            "-nolisten",
            "tcp",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


@contextmanager
def launch_persistent_profile(
    profile_dir: str | Path,
    *,
    headless: bool = False,
    xvfb_display: int | None = None,
    executable_path: str | None = None,
    args: list[str] | None = None,
    ignore_default_args: list[str] | None = None,
) -> Iterator[Any]:
    """Context manager yielding a Playwright persistent Chromium context.

    Cleans stale singleton locks before launch. Optionally starts Xvfb
    on the requested display. On exit, closes the context.

    `ignore_default_args` lets callers strip Playwright's default switches
    that some sites use to detect automation (notably --enable-automation,
    which Google Messages Web rejects during QR pairing). Forwarded to
    chromium.launch_persistent_context only when the caller passes it,
    so existing consumers see no behavior change.
    """
    profile = Path(profile_dir)
    ensure_profile_dir(profile)
    cleanup_profile_lock(profile)

    if xvfb_display is not None:
        ensure_xvfb(display_num=xvfb_display)

    launch_args = list(args) if args is not None else list(_DEFAULT_ARGS)

    launch_kwargs: dict[str, Any] = dict(
        user_data_dir=str(profile),
        headless=headless,
        args=launch_args,
    )
    # Only forward executable_path when the caller explicitly supplied
    # one. Passing None here would override Playwright's own default
    # resolution (which points at the bundled chromium under
    # ~/.cache/ms-playwright/). The old '/usr/bin/chromium' default
    # broke every consumer on any host that didn't have the system
    # package installed — which was the 2026-04-15 LinkedIn outage.
    if executable_path is not None:
        launch_kwargs["executable_path"] = executable_path
    if ignore_default_args is not None:
        launch_kwargs["ignore_default_args"] = list(ignore_default_args)

    with _sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            yield browser
        finally:
            try:
                browser.close()
            except Exception:
                pass
