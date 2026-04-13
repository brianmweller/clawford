#!/usr/bin/env bash
# install-host-cron.sh — idempotent install of all openclaw host crons.
#
# Adds (or verifies) crontab entries for the openclaw user that run
# work moved off the LLM dispatch queue onto plain host cron. Safe to
# re-run. Each entry is marked with a unique "# <marker>" comment so
# subsequent runs detect pre-existing installs.
#
# Registered entries (direct wrappers):
#   */5 * * * *  costco-token-refresh-host.sh   (every 5 min — Costco JWT)
#   0 12 * * *   morning-fleet-deliver-host.sh  (5:00 AM PDT — morning briefs)
#
# Registered entries (via generic script-contract-host.sh wrapper):
#   */30 * * * *  shopping-heartbeat              → SHOPPING_BOT_TOKEN
#   */30 * * * *  meetings-coach-heartbeat        → MEETINGS_BOT_TOKEN
#   */30 * * * *  fix-it-heartbeat-check          → TELEGRAM_BOT_TOKEN
#   0 */6 * * *   linkedin-keepalive              → NEWSDIGEST_BOT_TOKEN
#   */5 * * * *   family-calendar-reminder-check  → FAMILYCAL_BOT_TOKEN
#   */5 * * * *   news-digest-engagement-poll     → NEWSDIGEST_BOT_TOKEN
#
# Usage: ssh openclaw@198.51.100.42 "/home/openclaw/repo/ops/scripts/install-host-cron.sh"
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WRAPPER_DIR="$REPO_ROOT/ops/scripts"

# Direct wrappers — each with its own dedicated script.
# Format: "<schedule>|<wrapper_basename>|<marker>"
DIRECT_ENTRIES=(
  "*/5 * * * *|costco-token-refresh-host.sh|# costco-token-refresh-host"
  "0 12 * * *|morning-fleet-deliver-host.sh|# morning-fleet-deliver-host"
)

# Generic contract wrappers — use script-contract-host.sh with args.
# Format: "<schedule>|<logname>|<container-script-path>|<bot-token-env>|<timeout-s>"
# Marker is derived from logname: "# script-contract-<logname>"
CONTRACT_ENTRIES=(
  "*/30 * * * *|shopping-heartbeat|/home/node/.openclaw/shopping-workspace/scripts/heartbeat.py|SHOPPING_BOT_TOKEN|120"
  "*/30 * * * *|meetings-coach-heartbeat|/home/node/.openclaw/meetings-coach-workspace/scripts/heartbeat.py|MEETINGS_BOT_TOKEN|120"
  "*/30 * * * *|fix-it-heartbeat-check|/home/node/.openclaw/fix-it-workspace/scripts/heartbeat.py|TELEGRAM_BOT_TOKEN|120"
  "0 */6 * * *|linkedin-keepalive|/home/node/.openclaw/news-digest-workspace/scripts/linkedin-keepalive.py|NEWSDIGEST_BOT_TOKEN|300"
  "*/5 * * * *|family-calendar-reminder-check|/home/node/.openclaw/family-calendar-workspace/scripts/reminder-check.py|FAMILYCAL_BOT_TOKEN|90"
  "*/5 * * * *|news-digest-engagement-poll|/home/node/.openclaw/news-digest-workspace/scripts/engagement-poller.py|NEWSDIGEST_BOT_TOKEN|60"
)

NEW_LINES=()

# Process direct entries
for entry in "${DIRECT_ENTRIES[@]}"; do
  IFS='|' read -r schedule wrapper_name marker <<< "$entry"
  wrapper="$WRAPPER_DIR/$wrapper_name"

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

# Process generic contract entries
CONTRACT_WRAPPER="$WRAPPER_DIR/script-contract-host.sh"
if [[ ! -f "$CONTRACT_WRAPPER" ]]; then
  echo "[install-host-cron] MISSING: $CONTRACT_WRAPPER" >&2
  exit 1
fi
chmod +x "$CONTRACT_WRAPPER"

for entry in "${CONTRACT_ENTRIES[@]}"; do
  IFS='|' read -r schedule logname script_path token_env timeout_s <<< "$entry"
  marker="# script-contract-$logname"

  if crontab -l 2>/dev/null | grep -Fq "$marker"; then
    existing=$(crontab -l | grep -F "$marker")
    echo "[install-host-cron] already installed: $existing"
    continue
  fi

  NEW_LINES+=("$schedule $CONTRACT_WRAPPER $logname $script_path $token_env $timeout_s $marker")
done

if [[ ${#NEW_LINES[@]} -eq 0 ]]; then
  echo "[install-host-cron] all entries already registered — nothing to do"
  exit 0
fi

# Append new entries to the existing crontab (preserving any unrelated
# lines). Uses existing | new ordering so we never lose state on a
# partial write.
{
  crontab -l 2>/dev/null || true
  for line in "${NEW_LINES[@]}"; do
    echo "$line"
  done
} | crontab -

for line in "${NEW_LINES[@]}"; do
  echo "[install-host-cron] installed: $line"
done
