#!/usr/bin/env bash
# script-contract-host.sh — generic host wrapper for SCRIPT_CONTRACT scripts.
#
# Usage:
#   script-contract-host.sh <logname> <container-script-path> <bot-token-env> [<timeout-s>]
#
# Example crontab:
#   */30 * * * * /home/openclaw/repo/ops/scripts/script-contract-host.sh \
#     shopping-heartbeat \
#     /home/node/.openclaw/shopping-workspace/scripts/heartbeat.py \
#     SHOPPING_BOT_TOKEN
#
# WHAT IT DOES
# ------------
# Runs any SCRIPT_CONTRACT-compliant Python script inside the openclaw
# gateway container via `docker exec`, parses the final JSON line of
# stdout, and on `status != "ok"` relays the `alert` field to Telegram
# using the specified agent bot token. Silent on success (per contract).
#
# This is the generic equivalent of what the openclaw LLM cron was
# doing for every heartbeat / keepalive / validator cron: run the
# script, read the JSON, forward the alert. Moving these off the LLM
# dispatch path removes 12+ slots/hour of queue contention during the
# morning burst window.
#
# The script lives on the host but invokes the target script INSIDE
# the container — that's where all the workspace paths, env vars,
# and Python dependencies are. Host cron is just the dispatcher.
#
# LOG:   ~/.openclaw/logs/<logname>-host.log (rotated @ 1 MB)
# LOCK:  /tmp/<logname>-host.lock (flock, non-blocking)
set -u

LOGNAME="${1:?usage: $0 <logname> <container-script-path> <bot-token-env> [timeout-s]}"
SCRIPT="${2:?usage: $0 <logname> <container-script-path> <bot-token-env> [timeout-s]}"
TOKEN_ENV="${3:?usage: $0 <logname> <container-script-path> <bot-token-env> [timeout-s]}"
TIMEOUT_S="${4:-120}"

CONTAINER="openclaw-openclaw-gateway-1"
LOG_FILE="/home/openclaw/.openclaw/logs/${LOGNAME}-host.log"
LOCK_FILE="/tmp/${LOGNAME}-host.lock"
ENV_FILE="/home/openclaw/openclaw/.env"

mkdir -p "$(dirname "$LOG_FILE")"

# Serialize: no overlap. Non-blocking — if a previous run is hung,
# skip this tick rather than pile up.
exec 200>"$LOCK_FILE"
flock -n 200 || {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $LOGNAME skipped (lock held)" >> "$LOG_FILE"
  exit 0
}

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

OUTPUT=$(timeout "$TIMEOUT_S" docker exec "$CONTAINER" python3 "$SCRIPT" 2>&1)
EXIT_CODE=$?

{
  echo "[$TS] $LOGNAME exit=$EXIT_CODE"
  echo "$OUTPUT" | tail -5
} >> "$LOG_FILE"

# Extract the LAST line of stdout — per SCRIPT_CONTRACT, the final
# line is a single JSON object. Use Python to parse it safely rather
# than jq (which isn't guaranteed installed on the host).
LAST_LINE=$(echo "$OUTPUT" | tail -1)
STATUS=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('status', ''))
except Exception:
    print('')
" "$LAST_LINE" 2>/dev/null || echo "")

if [[ "$STATUS" != "ok" ]] && [[ -n "$STATUS" ]]; then
  # Prefer the 'alert' field (SCRIPT_CONTRACT v2 shape, used by
  # heartbeat.py). Fall back to 'message' (v1 shape, used by
  # linkedin-keepalive.py etc.) so older scripts that predate the
  # contract still relay a useful Telegram body.
  ALERT=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    msg = d.get('alert') or d.get('message') or ''
    print(msg)
except Exception:
    print('')
" "$LAST_LINE" 2>/dev/null || echo "")

  if [[ -n "$ALERT" ]] && [[ -f "$ENV_FILE" ]]; then
    # Source the host .env to get all bot tokens + TELEGRAM_CHAT_ID.
    # `set -a` auto-exports every variable sourced, so the `${!var}`
    # indirect lookup below finds them.
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a

    BOT_TOKEN="${!TOKEN_ENV:-}"
    if [[ -n "$BOT_TOKEN" ]] && [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
      HTTP_STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
        -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${ALERT}") || HTTP_STATUS="000"
      echo "[$TS] $LOGNAME alert sent (http=$HTTP_STATUS): ${ALERT:0:120}" >> "$LOG_FILE"
    else
      echo "[$TS] $LOGNAME alert dropped — $TOKEN_ENV or TELEGRAM_CHAT_ID not set" >> "$LOG_FILE"
    fi
  fi
fi

# Rotate log at 1 MB
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

exit 0
