#!/usr/bin/env bash
# fleet-health-host.sh — host-side wrapper for the fleet-health
# orchestrator. Runs DIRECTLY on the host (NOT inside the openclaw
# gateway container) because the orchestrator itself uses
# `docker exec` to probe each agent, and the container has no
# docker binary.
#
# script-contract-host.sh is the wrong shape for this script: it
# wraps the script in `docker exec`, which would make fleet-health.py
# run inside the container where docker is unavailable. Hence this
# dedicated direct wrapper.
#
# INSTALL: ops/scripts/install-host-cron.sh registers this in
# DIRECT_ENTRIES with marker "# fleet-health-host".
# LOG:     ~/.openclaw/logs/fleet-health-host.log (rotated @ 1 MB)
# LOCK:    /tmp/fleet-health-host.lock (flock, non-blocking)
set -u

ORCHESTRATOR="/home/openclaw/repo/ops/scripts/fleet-health.py"
LOG_FILE="/home/openclaw/.openclaw/logs/fleet-health-host.log"
LOCK_FILE="/tmp/fleet-health-host.lock"
ENV_FILE="/home/openclaw/openclaw/.env"

mkdir -p "$(dirname "$LOG_FILE")"

# Serialize: skip if a previous run is still going (60s timeout in
# the script + 6 agents × 60s per-agent ceiling = max ~6 min).
exec 200>"$LOCK_FILE"
flock -n 200 || {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fleet-health skipped (lock held)" >> "$LOG_FILE"
  exit 0
}

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

OUTPUT=$(timeout 600 /usr/bin/python3 "$ORCHESTRATOR" 2>&1)
EXIT_CODE=$?

{
  echo "[$TS] fleet-health exit=$EXIT_CODE"
  echo "$OUTPUT" | tail -10
} >> "$LOG_FILE"

# Parse the LAST line of stdout as JSON per SCRIPT_CONTRACT.
LAST_LINE=$(echo "$OUTPUT" | tail -1)
STATUS=$(/usr/bin/python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('status', ''))
except Exception:
    print('')
" "$LAST_LINE" 2>/dev/null || echo "")

if [[ "$STATUS" != "ok" ]] && [[ -n "$STATUS" ]]; then
  ALERT=$(/usr/bin/python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('alert') or d.get('error') or '')
except Exception:
    print('')
" "$LAST_LINE" 2>/dev/null || echo "")

  if [[ -n "$ALERT" ]] && [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a

    BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
    if [[ -n "$BOT_TOKEN" ]] && [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
      HTTP_STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
        -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${ALERT}") || HTTP_STATUS="000"
      echo "[$TS] fleet-health alert sent (http=$HTTP_STATUS): ${ALERT:0:120}" >> "$LOG_FILE"
    else
      echo "[$TS] fleet-health alert dropped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set" >> "$LOG_FILE"
    fi
  fi
fi

# Rotate log at 1 MB
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

exit 0
