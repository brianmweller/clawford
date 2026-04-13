#!/usr/bin/env bash
# install-host-cron.sh — idempotent install of all openclaw host crons.
#
# Adds (or verifies) crontab entries for the openclaw user that run
# work moved off the LLM dispatch queue onto plain host cron. Safe to
# re-run. Each entry is marked with a unique "# <marker>" comment so
# subsequent runs detect pre-existing installs.
#
# Registered entries:
#   */5 * * * *  costco-token-refresh-host.sh     (every 5 minutes — JWT keepalive)
#   0 12 * * *   morning-fleet-deliver-host.sh    (5:00 AM PDT — morning briefs)
#
# Usage: ssh openclaw@198.51.100.42 "/home/openclaw/repo/ops/scripts/install-host-cron.sh"
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)

# Tuples of (schedule, wrapper_basename, marker). Order matters only for
# log output; cron applies them independently.
ENTRIES=(
  "*/5 * * * *|costco-token-refresh-host.sh|# costco-token-refresh-host"
  "0 12 * * *|morning-fleet-deliver-host.sh|# morning-fleet-deliver-host"
)

NEW_LINES=()
for entry in "${ENTRIES[@]}"; do
  IFS='|' read -r schedule wrapper_name marker <<< "$entry"
  wrapper="$REPO_ROOT/ops/scripts/$wrapper_name"

  if [[ ! -f "$wrapper" ]]; then
    echo "[install-host-cron] MISSING: $wrapper" >&2
    exit 1
  fi
  chmod +x "$wrapper"

  if crontab -l 2>/dev/null | grep -Fq "$marker"; then
    existing=$(crontab -l | grep -F "$marker")
    echo "[install-host-cron] already installed: $existing"
    continue
  fi

  NEW_LINES+=("$schedule $wrapper $marker")
done

if [[ ${#NEW_LINES[@]} -eq 0 ]]; then
  echo "[install-host-cron] all entries already registered — nothing to do"
  exit 0
fi

# Append new entries to the existing crontab (preserving any unrelated
# lines). Uses a heredoc + existing | new ordering so we never lose
# state on a partial write.
{
  crontab -l 2>/dev/null || true
  for line in "${NEW_LINES[@]}"; do
    echo "$line"
  done
} | crontab -

for line in "${NEW_LINES[@]}"; do
  echo "[install-host-cron] installed: $line"
done
