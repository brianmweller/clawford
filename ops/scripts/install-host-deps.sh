#!/usr/bin/env bash
# install-host-deps.sh — idempotent host-Python dep bootstrap for Clawford.
#
# Phase 6.5 needs every Python package the openclaw gateway container
# had installed to be present on the host as well, because the host-cron
# wrappers no longer `docker exec` into the container — they invoke
# /usr/bin/python3 directly against bind-mounted scripts.
#
# PREREQUISITE: run install-host-system-deps.sh once first (with sudo).
# Several of the Python packages installed below — pillow transitively,
# camoufox's headful fallback — need apt packages (libjpeg-dev,
# libfreetype6-dev, zlib1g-dev, libpng-dev, xvfb, openbox) that the
# system-deps script installs.
#
# Install mode: `pip install --user --break-system-packages`.
#   --user lands packages under ~/.local/lib/python3.12/site-packages/
#   --break-system-packages opts into installing into the system-managed
#      Python interpreter on Debian 12+/Ubuntu 24+ (PEP 668). The host
#      already has 60+ packages installed under ~/.local/ via this
#      pattern, so this is consistent with the existing layout.
#
# Rerunnable: pip install short-circuits when every package is up to
# date, and `playwright install chromium` + `camoufox fetch` both
# short-circuit when their browsers are already on disk.
#
# USAGE:
#   ssh openclaw@<vps> "~/repo/ops/scripts/install-host-deps.sh"
#
# On success the script prints `[host-deps] ok — all imports resolved`
# as its last line. Any ModuleNotFoundError or browser-fetch failure
# aborts with a non-zero exit.

set -euo pipefail

REQUIREMENTS_FILE="$(dirname "$0")/requirements-host.txt"

if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  echo "[host-deps] ERROR: requirements-host.txt not found at $REQUIREMENTS_FILE" >&2
  exit 1
fi

echo "[host-deps] installing Python packages via pip --user..."
/usr/bin/python3 -m pip install \
  --user \
  --break-system-packages \
  --disable-pip-version-check \
  -r "$REQUIREMENTS_FILE"

echo "[host-deps] installing Playwright chromium browser..."
/usr/bin/python3 -m playwright install chromium

echo "[host-deps] fetching Camoufox browser..."
# camoufox fetch writes under ~/.cache/camoufox/. Skip if a cached
# binary already exists — as of 2026-04-27 upstream Camoufox moved
# past the beta-N tag scheme (latest tag is v146-*) and the GitHub
# Releases JSON API returns [] for the daijro/camoufox repo, so
# `camoufox fetch` always raises MissingRelease against the pinned
# lib's `(>=beta.19, <1)` range. The runtime launch path doesn't
# invoke pkgman when the cache is populated, so this is a deploy-
# time concern only. Re-running fetch on a fresh host without a
# cached binary will surface the upstream issue and fail loudly,
# which is the correct behavior.
CAMOUFOX_BIN="$HOME/.cache/camoufox/camoufox-bin"
if [[ -x "$CAMOUFOX_BIN" ]]; then
  echo "[host-deps] camoufox cache populated at $CAMOUFOX_BIN — skipping fetch"
else
  /usr/bin/python3 -m camoufox fetch
fi

echo "[host-deps] verifying imports..."
/usr/bin/python3 - <<'PY'
import importlib
import sys

REQUIRED = [
    "feedparser",
    "linkedin_api",
    "googlenewsdecoder",
    "pyotp",
    "playwright",
    "camoufox",
    "rebrowser_playwright",
    "amazonorders",  # pip name: amazon-orders; PyPI installs as amazonorders
    "openai",
    "curl_cffi",
    "googleapiclient",
    "google_auth_oauthlib",
    "google.auth.transport.requests",
    "mcp",
    "httpx",
    "httpx_sse",
    "bs4",
    "lxml",
    "dateutil",
    "pytz",
    "fastembed",  # 2026-04-21 — Phase 3 local embedding for fact dedupe
    # stdlib / already-on-host sanity
    "json",
    "subprocess",
]

missing = []
for name in REQUIRED:
    try:
        importlib.import_module(name)
    except ImportError as e:
        missing.append((name, str(e)))

if missing:
    print("[host-deps] FAILED — missing imports:")
    for name, err in missing:
        print(f"  - {name}: {err}")
    sys.exit(1)

print("[host-deps] ok — all imports resolved")
PY

echo "[host-deps] verifying Camoufox launch (cache + lib version coupling)..."
# Camoufox's pip release tightly couples the Python library to a specific
# upstream Firefox-fork release range. When the library's range stops
# matching what's in ~/.cache/camoufox/ (or what GitHub still publishes),
# every browser cron silently fails with "No matching release found for
# lin x86_64 in the supported range". Fail the install here so version
# drift surfaces at deploy time, not 6 hours later in a cron alert.
# Regression target: 2026-04-27 connector-gmessages-mine alert.
/usr/bin/python3 - <<'PY'
import sys
try:
    from camoufox.sync_api import Camoufox
    # headless="virtual" mirrors gmessages-mine, costco-token-daemon,
    # amazon-auth, etc. — they spawn Xvfb (installed by
    # install-host-system-deps.sh) so the smoke check exercises the
    # exact launch path the fleet uses.
    with Camoufox(headless="virtual") as browser:
        page = browser.new_page()
        page.goto("about:blank")
    print("[host-deps] Camoufox launch ok")
except Exception as e:
    print(f"[host-deps] FAILED — Camoufox launch broken: {e}")
    print("[host-deps] hint: pin camoufox in requirements-host.txt to a")
    print("[host-deps] version compatible with the cached browser at")
    print("[host-deps] ~/.cache/camoufox/, or rerun '/usr/bin/python3 -m camoufox fetch'")
    sys.exit(1)
PY
