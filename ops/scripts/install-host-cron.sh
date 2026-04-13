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
#   */15 * * * *  fleet-health                    → TELEGRAM_BOT_TOKEN  (R3 — replaces per-agent heartbeats)
#   0 */6 * * *   linkedin-keepalive              → NEWSDIGEST_BOT_TOKEN
#   */5 * * * *   family-calendar-reminder-check  → FAMILYCAL_BOT_TOKEN
#   */5 * * * *   news-digest-engagement-poll     → NEWSDIGEST_BOT_TOKEN
#
# Removed in R3 (replaced by fleet-health):
#   */30 * * * *  shopping-heartbeat              → covered by fleet-health
#   */30 * * * *  meetings-coach-heartbeat        → covered by fleet-health
#   */30 * * * *  fix-it-heartbeat-check          → covered by fleet-health
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
  "*/15 * * * *|fleet-health-host.sh|# fleet-health-host"
  "30 10 * * *|morning-status-host.sh|# morning-status-host"
)

# Generic contract wrappers — use script-contract-host.sh with args.
# Format: "<schedule>|<logname>|<container-script-path>|<bot-token-env>|<timeout-s>"
# Marker is derived from logname: "# script-contract-<logname>"
CONTRACT_ENTRIES=(
  "0 */6 * * *|linkedin-keepalive|/home/node/.openclaw/news-digest-workspace/scripts/linkedin-keepalive.py|NEWSDIGEST_BOT_TOKEN|300"
  "*/5 * * * *|family-calendar-reminder-check|/home/node/.openclaw/family-calendar-workspace/scripts/reminder-check.py|FAMILYCAL_BOT_TOKEN|90"
  "*/5 * * * *|news-digest-engagement-poll|/home/node/.openclaw/news-digest-workspace/scripts/engagement-poller.py|NEWSDIGEST_BOT_TOKEN|60"
)

# Markers for old entries to REMOVE on next install run. Used by the
# remove-stale step below — any crontab line containing one of these
# markers is dropped before adding the new CONTRACT_ENTRIES. This
# closes the install-host-cron.sh "yo-yo" gap from the R3 transition.
STALE_MARKERS=(
  "# script-contract-shopping-heartbeat"
  "# script-contract-meetings-coach-heartbeat"
  "# script-contract-fix-it-heartbeat-check"
  "# script-contract-fleet-health"
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

# Drop any stale crontab lines whose marker matches STALE_MARKERS.
# This handles the install-host-cron.sh "yo-yo" case where a prior
# version of this script registered an entry that's since been
# replaced (e.g. R3 replacing 3 per-agent heartbeat crons with one
# fleet-health entry). Without this the operator would have to
# `crontab -e` manually to remove the old lines.
STALE_REMOVED=0
if [[ ${#STALE_MARKERS[@]} -gt 0 ]]; then
  CURRENT=$(crontab -l 2>/dev/null || true)
  if [[ -n "$CURRENT" ]]; then
    FILTERED="$CURRENT"
    for marker in "${STALE_MARKERS[@]}"; do
      if echo "$FILTERED" | grep -Fq "$marker"; then
        FILTERED=$(echo "$FILTERED" | grep -vF "$marker")
        STALE_REMOVED=$((STALE_REMOVED + 1))
        echo "[install-host-cron] removed stale entry: $marker"
      fi
    done
    if [[ "$STALE_REMOVED" -gt 0 ]]; then
      echo "$FILTERED" | crontab -
    fi
  fi
fi

if [[ ${#NEW_LINES[@]} -eq 0 ]] && [[ "$STALE_REMOVED" -eq 0 ]]; then
  echo "[install-host-cron] all entries already registered — nothing to do"
  exit 0
fi

if [[ ${#NEW_LINES[@]} -eq 0 ]]; then
  echo "[install-host-cron] removed $STALE_REMOVED stale entries; no new lines to add"
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
