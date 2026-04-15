#!/usr/bin/env bash
# fix-it-cron-self-check-host.sh — host-side wrapper for cron-self-check.py.
# Runs DIRECTLY on the host (NOT inside the openclaw gateway container)
# because cron-self-check.py reads:
#   - `crontab -l` from the host crontab (the container has its own
#     empty crontab and no way to ask the host)
#   - install-host-cron.sh from /home/openclaw/repo/ops/scripts/
#     (lives on the host, mounted into the container at a different
#     path so a single hard-coded path can't satisfy both runtimes)
#
# Mirrors the fleet-health-host.sh shape: run script on host, parse
# the JSON tail of stdout for `status` and `alert`, curl any non-empty
# alert to Telegram. Bot token sourced from /home/openclaw/openclaw/.env.
#
# INSTALL: ops/scripts/install-host-cron.sh registers this in
# DIRECT_ENTRIES with marker "# fix-it-cron-self-check-host".
# LOG:     ~/.openclaw/logs/fix-it-cron-self-check-host.log (rotated @ 1 MB)
# LOCK:    /tmp/fix-it-cron-self-check-host.lock (flock, non-blocking)
set -u

ORCHESTRATOR="/home/openclaw/repo/agents/fix-it/scripts/cron-self-check.py"
LOG_FILE="/home/openclaw/.openclaw/logs/fix-it-cron-self-check-host.log"
LOCK_FILE="/tmp/fix-it-cron-self-check-host.lock"
ENV_FILE="/home/openclaw/openclaw/.env"

mkdir -p "$(dirname "$LOG_FILE")"

exec 200>"$LOCK_FILE"
flock -n 200 || {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cron-self-check skipped (lock held)" >> "$LOG_FILE"
  exit 0
}

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

OUTPUT=$(timeout 60 /usr/bin/python3 "$ORCHESTRATOR" 2>&1)
EXIT_CODE=$?

{
  echo "[$TS] cron-self-check exit=$EXIT_CODE"
  echo "$OUTPUT" | tail -10
} >> "$LOG_FILE"

LAST_LINE=$(echo "$OUTPUT" | tail -1)
ALERT=$(/usr/bin/python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('alert') or '')
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
    echo "[$TS] cron-self-check alert sent (http=$HTTP_STATUS): ${ALERT:0:120}" >> "$LOG_FILE"
  else
    echo "[$TS] cron-self-check alert dropped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set" >> "$LOG_FILE"
  fi
fi

if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

exit 0
