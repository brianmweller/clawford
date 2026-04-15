#!/usr/bin/env bash
# morning-status-host.sh — host-side wrapper for the R4 fix-it
# morning-status.py classifier. Runs DIRECTLY on the host (not
# via docker exec) because the script reads/writes Dropbox-mounted
# files directly: ~/Dropbox/openclaw-backup/fleet-health.json,
# ~/Dropbox/openclaw-backup/fix-it/KNOWN_ISSUES.md, validate.py,
# and writes ~/.clawford/fix-it-workspace/cache/morning-brief-ready.txt.
#
# All paths are bind-mounted into the container too, so morning-fleet-
# deliver.py at 12:00 UTC sees the same cache file the script wrote
# at 10:30 UTC from the host.
#
# INSTALL: ops/scripts/install-host-cron.sh registers this in
# DIRECT_ENTRIES with marker "# morning-status-host".
# LOG:     ~/.clawford/logs/morning-status-host.log (rotated @ 1 MB)
# LOCK:    /tmp/morning-status-host.lock (flock, non-blocking)
set -u

SCRIPT="/home/openclaw/repo/agents/fix-it/scripts/morning-status.py"
LOG_FILE="/home/openclaw/.clawford/logs/morning-status-host.log"
LOCK_FILE="/tmp/morning-status-host.lock"
ENV_FILE="/home/openclaw/clawford/.env"

mkdir -p "$(dirname "$LOG_FILE")"

exec 200>"$LOCK_FILE"
flock -n 200 || {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] morning-status skipped (lock held)" >> "$LOG_FILE"
  exit 0
}

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# 120s ceiling: validate.py takes ~5s, find conflicts ~2s, classification
# is instant, file write is instant. 120s is generous.
OUTPUT=$(timeout 120 /usr/bin/python3 "$SCRIPT" 2>&1)
EXIT_CODE=$?

{
  echo "[$TS] morning-status exit=$EXIT_CODE"
  echo "$OUTPUT" | tail -10
} >> "$LOG_FILE"

# Parse last line as JSON. morning-status.py emits SCRIPT_CONTRACT JSON.
LAST_LINE=$(echo "$OUTPUT" | tail -1)
STATUS=$(/usr/bin/python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('status', ''))
except Exception:
    print('')
" "$LAST_LINE" 2>/dev/null || echo "")

# Note: morning-status alerting is intentionally MUTED — the report
# itself goes through morning-fleet-deliver at 12:00 UTC via the
# normal Telegram pipeline. We only alert the operator if the SCRIPT
# itself crashed (status=error), not on degraded fleet state (which
# is what the report communicates).
if [[ "$STATUS" == "error" ]] && [[ -f "$ENV_FILE" ]]; then
  ALERT=$(/usr/bin/python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('alert') or d.get('error') or '')
except Exception:
    print('')
" "$LAST_LINE" 2>/dev/null || echo "")

  if [[ -n "$ALERT" ]]; then
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
      echo "[$TS] morning-status alert sent (http=$HTTP_STATUS): ${ALERT:0:120}" >> "$LOG_FILE"
    fi
  fi
fi

if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

exit 0
