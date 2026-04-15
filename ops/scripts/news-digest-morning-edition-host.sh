#!/usr/bin/env bash
# news-digest-morning-edition-host.sh — Lowly Worm's morning compose step.
#
# Replaces the OpenClaw LLM cron that used to run fetch-and-rank.py
# and then compose cache/morning-items.json via an LLM session inside
# the cron runtime. Under Phase 3b this is pure host cron + two
# Python scripts:
#
#   1. fetch-and-rank.py  — fetches RSS + LinkedIn, ranks against the
#                           preference model, writes ranked-<date>.json.
#                           Also summarizes LinkedIn threads via
#                           agents.shared.llm.infer().
#
#   2. morning-edition.py — reads the ranked file, calls llm.infer()
#                           with json_mode=True to compose the
#                           structured items array, writes
#                           cache/morning-items.json.
#
# The separate morning-fleet-deliver-host.sh cron at 12:00 UTC reads
# morning-items.json and sends each item as its own Telegram message
# with inline-keyboard reactions.
#
# Fires at 30 10 UTC (matches the OpenClaw cron schedule). The delivery
# wrapper fires at 12:00 UTC, leaving 90 minutes of slack for any
# fetch/infer latency spikes.
#
# INSTALL: ops/scripts/install-host-cron.sh (idempotent).
# LOG:     ~/.openclaw/logs/news-digest-morning-edition-host.log (1 MB rotation)
# LOCK:    /tmp/news-digest-morning-edition-host.lock (flock non-blocking)
set -u

FETCH_AND_RANK="/home/openclaw/.openclaw/news-digest-workspace/scripts/fetch-and-rank.py"
MORNING_EDITION="/home/openclaw/.openclaw/news-digest-workspace/scripts/morning-edition.py"
LOG_FILE="/home/openclaw/.openclaw/logs/news-digest-morning-edition-host.log"
LOCK_FILE="/tmp/news-digest-morning-edition-host.lock"
ENV_FILE="/home/openclaw/openclaw/.env"

mkdir -p "$(dirname "$LOG_FILE")"

# Non-blocking lock — if a previous run is still going (fetch is slow
# some days), skip this tick rather than pile up. Next day's cron retries.
exec 200>"$LOCK_FILE"
flock -n 200 || {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] skipped (lock held)" >> "$LOG_FILE"
  exit 0
}

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
  echo "[$TS] === morning-edition start ==="
} >> "$LOG_FILE"

# Source the host .env once up front so NEWSDIGEST_BOT_TOKEN,
# LINKEDIN_*, TELEGRAM_CHAT_ID, and the codex auth path are in the
# subprocess environment. Pre-6.5 docker exec inherited them from
# the container — now the host wrapper is responsible.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Step 1: fetch-and-rank. 10 min ceiling — RSS is usually <2 min but
# LinkedIn scraper can block, and the LinkedIn thread summary loop
# calls llm.infer() per unread thread.
FETCH_OUT=$(timeout 600 /usr/bin/python3 "$FETCH_AND_RANK" 2>&1)
FETCH_RC=$?
{
  echo "[$TS] fetch-and-rank exit=$FETCH_RC"
  echo "$FETCH_OUT" | tail -5
} >> "$LOG_FILE"

if [[ "$FETCH_RC" -ne 0 ]]; then
  echo "[$TS] fetch-and-rank failed — skipping morning-edition compose" >> "$LOG_FILE"
  # Alert via NEWSDIGEST_BOT_TOKEN — .env already sourced above.
  if [[ -n "${NEWSDIGEST_BOT_TOKEN:-}" ]] && [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -s -o /dev/null -w '%{http_code}' \
      -X POST "https://api.telegram.org/bot${NEWSDIGEST_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=🐛 morning-edition: fetch-and-rank failed (exit $FETCH_RC)" >> "$LOG_FILE" 2>&1 || true
    echo "" >> "$LOG_FILE"
  fi
  exit 0
fi

# Step 2: morning-edition LLM compose. 3 min ceiling — one llm.infer()
# call with a ~5k-token prompt, usually returns in 30-60s.
COMPOSE_OUT=$(timeout 180 /usr/bin/python3 "$MORNING_EDITION" 2>&1)
COMPOSE_RC=$?
{
  echo "[$TS] morning-edition exit=$COMPOSE_RC"
  echo "$COMPOSE_OUT" | tail -5
} >> "$LOG_FILE"

# Parse the JSON status line. SCRIPT_CONTRACT: final non-blank stdout
# line is a single JSON object with a `status` field.
LAST_LINE=$(echo "$COMPOSE_OUT" | tail -1)
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

  # .env already sourced at the top of the script.
  if [[ -n "$ALERT" ]]; then
    if [[ -n "${NEWSDIGEST_BOT_TOKEN:-}" ]] && [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
      HTTP_STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
        -X POST "https://api.telegram.org/bot${NEWSDIGEST_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${ALERT}") || HTTP_STATUS="000"
      echo "[$TS] alert sent (http=$HTTP_STATUS): ${ALERT:0:120}" >> "$LOG_FILE"
    fi
  fi
fi

# Log rotation at 1 MB
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

exit 0
