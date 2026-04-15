#!/usr/bin/env bash
# install-host-deps.sh — idempotent host-Python dep bootstrap for Clawford.
#
# Phase 6.5 needs every Python package the openclaw gateway container
# had installed to be present on the host as well, because the host-cron
# wrappers no longer `docker exec` into the container — they invoke
# /usr/bin/python3 directly against bind-mounted scripts.
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
# camoufox fetch writes under ~/.cache/camoufox/. Safe to rerun — it
# short-circuits when the browser is already on disk.
/usr/bin/python3 -m camoufox fetch

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
