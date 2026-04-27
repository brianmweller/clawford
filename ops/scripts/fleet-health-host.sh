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
# LOG:     ~/.clawford/logs/fleet-health-host.log (rotated @ 1 MB)
# LOCK:    /tmp/fleet-health-host.lock (flock, non-blocking)
set -u

ORCHESTRATOR="/home/openclaw/repo/ops/scripts/fleet-health.py"
LOG_FILE="/home/openclaw/.clawford/logs/fleet-health-host.log"
LOCK_FILE="/tmp/fleet-health-host.lock"
ENV_FILE="/home/openclaw/clawford/.env"

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

# Run the alert gate on every tick — not just degraded ones. The gate
# needs to see "ok" transitions to emit recovery messages, and it
# silently no-ops when the state is unchanged. Gate prints the message
# to send on stdout; empty stdout = suppress.
GATE="/home/openclaw/repo/ops/scripts/fleet_alert_gate.py"
if [[ -n "$LAST_LINE" ]] && [[ -f "$GATE" ]]; then
  MESSAGE=$(/usr/bin/python3 "$GATE" "$LAST_LINE" 2>>"$LOG_FILE")
else
  MESSAGE=""
fi

if [[ -n "$MESSAGE" ]] && [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a

  # Log the alert text to fix-it's conversation window so the LLM can
  # see its own outbound when the operator replies later. Best-effort —
  # failure here must not break the Telegram delivery below.
  /usr/bin/python3 -c "
import sys
sys.path.insert(0, '/home/openclaw/repo')
from agents.shared import conversation
conversation.append('fix-it', {'role': 'assistant', 'content': sys.argv[1]})
" "$MESSAGE" 2>>"$LOG_FILE" || true

  BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
  if [[ -n "$BOT_TOKEN" ]] && [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    HTTP_STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
      -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=${MESSAGE}") || HTTP_STATUS="000"
    echo "[$TS] fleet-health alert sent (http=$HTTP_STATUS): ${MESSAGE:0:120}" >> "$LOG_FILE"
  else
    echo "[$TS] fleet-health alert dropped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set" >> "$LOG_FILE"
  fi
fi

# Rotate log at 1 MB
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

exit 0
