#!/usr/bin/env bash
# install-host-cron.sh — idempotent install of the Costco refresh host cron.
#
# Adds (or verifies) a `*/5 * * * *` crontab entry for the openclaw
# user that runs costco-token-refresh-host.sh. Safe to re-run.
#
# Usage: ssh openclaw@198.51.100.42 "/home/openclaw/repo/ops/scripts/install-host-cron.sh"
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WRAPPER="$REPO_ROOT/ops/scripts/costco-token-refresh-host.sh"
MARKER="# costco-token-refresh-host"
CRON_LINE="*/5 * * * * $WRAPPER $MARKER"

if [[ ! -x "$WRAPPER" ]]; then
  chmod +x "$WRAPPER"
fi

if crontab -l 2>/dev/null | grep -Fq "$MARKER"; then
  echo "[install-host-cron] already installed"
  crontab -l | grep -F "$MARKER"
  exit 0
fi

(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
echo "[install-host-cron] installed:"
echo "  $CRON_LINE"
