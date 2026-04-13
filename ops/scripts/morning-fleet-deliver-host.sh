#!/usr/bin/env bash
# morning-fleet-deliver-host.sh — host-level 5:00 AM PDT fleet delivery.
#
# WHY THIS EXISTS
# ---------------
# The 5 morning gather crons (shopping/family-calendar/meetings-coach/
# news-digest/fix-it) write pre-formatted briefs to each agent's
# cache/morning-brief-ready.txt. fix-it/morning-fleet-deliver.py then
# reads all 5 at 12:00 UTC, holds until exactly 12:00, and sends each
# via its agent bot token.
#
# On the openclaw LLM path, the 12:00 UTC slot is serialized behind
# the morning-burst queue (news-digest morning-edition 340 s and
# connector morning-relationship 580 s), routinely firing 3-5 minutes
# late. 2026-04-13 sample: morning-fleet-deliver dispatched at
# 12:05:05 → user received "5 AM PDT" delivery at 5:05 AM.
#
# Running the same script under plain host cron dodges the LLM
# queue entirely. The wrapper just `docker exec`s into the gateway
# container (where the agent bot tokens + TELEGRAM_CHAT_ID live in
# the process env) and lets the script do its thing. Because the
# script has its own hold-until-12:00 barrier, firing the host cron
# at exactly 0 12 * * * is fine — the barrier becomes a no-op and
# delivery happens at the wall-clock target.
#
# The script's per-day idempotency marker (morning-fleet-delivered-
# YYYY-MM-DD.json) protects against double-fire if the wrapper is
# ever manually re-triggered the same morning.
#
# INSTALL:  ops/scripts/install-host-cron.sh (idempotent)
# LOG:      ~/.openclaw/logs/morning-fleet-deliver-host.log (rotated @ 1 MB)
set -u

CONTAINER="openclaw-openclaw-gateway-1"
DELIVER="/home/node/.openclaw/fix-it-workspace/scripts/morning-fleet-deliver.py"
LOG_FILE="/home/openclaw/.openclaw/logs/morning-fleet-deliver-host.log"
LOCK_FILE="/tmp/morning-fleet-deliver-host.lock"

mkdir -p "$(dirname "$LOG_FILE")"

# Serialize: no overlap if a previous invocation is somehow still
# holding the 20-minute max wait window.
exec 200>"$LOCK_FILE"
flock -n 200 || {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] skipped (previous run still holds lock)" >> "$LOG_FILE"
  exit 0
}

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# 25-minute ceiling. The script's internal MAX_HOLD_SECONDS is 20 min,
# the Telegram sends themselves take <10 seconds total, so 25 min is
# a generous upper bound. If we somehow exceed it, abort and let
# the next morning's cron retry.
OUTPUT=$(timeout 1500 docker exec "$CONTAINER" python3 "$DELIVER" 2>&1)
EXIT_CODE=$?

{
  echo "[$TS] exit=$EXIT_CODE"
  echo "$OUTPUT" | tail -20
} >> "$LOG_FILE"

# Rotate log if > 1 MB. Keep one backup (~.1) so a week of data
# survives without unbounded growth.
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

# Exit 0 even on failure — the heartbeat / absence-of-delivery signals
# alert Sam independently. Don't spam the cron MAILTO.
exit 0
