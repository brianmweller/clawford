#!/usr/bin/env bash
# script-contract-host.sh — generic host wrapper for SCRIPT_CONTRACT scripts.
#
# Usage:
#   script-contract-host.sh <logname> <host-script-path> <bot-token-env> [<timeout-s>]
#
# Example crontab:
#   */30 * * * * /home/openclaw/repo/ops/scripts/script-contract-host.sh \
#     shopping-heartbeat \
#     /home/openclaw/.clawford/shopping-workspace/scripts/heartbeat.py \
#     SHOPPING_BOT_TOKEN
#
# WHAT IT DOES
# ------------
# Runs any SCRIPT_CONTRACT-compliant Python script as a bare host
# subprocess, parses the final JSON line of stdout, and on
# `status != "ok"` relays the `alert` field to Telegram using the
# specified agent bot token. Silent on success (per contract).
#
# Phase 6.5: the script used to run inside the openclaw gateway
# container via `docker exec`. Post-migration, agent Python runs
# on the host directly — the workspace filesystem is the same
# bind-mounted directory tree, and the host has the same Python
# dependencies installed via ops/scripts/install-host-deps.sh.
#
# LOG:   ~/.clawford/logs/<logname>-host.log (rotated @ 1 MB)
# LOCK:  /tmp/<logname>-host.lock (flock, non-blocking)
set -u

LOGNAME="${1:?usage: $0 <logname> <host-script-path> <bot-token-env> [timeout-s]}"
SCRIPT="${2:?usage: $0 <logname> <host-script-path> <bot-token-env> [timeout-s]}"
TOKEN_ENV="${3:?usage: $0 <logname> <host-script-path> <bot-token-env> [timeout-s]}"
TIMEOUT_S="${4:-120}"

LOG_FILE="/home/openclaw/.clawford/logs/${LOGNAME}-host.log"
LOCK_FILE="/tmp/${LOGNAME}-host.lock"
ENV_FILE="/home/openclaw/clawford/.env"

mkdir -p "$(dirname "$LOG_FILE")"

# Serialize: no overlap. Non-blocking — if a previous run is hung,
# skip this tick rather than pile up.
exec 200>"$LOCK_FILE"
flock -n 200 || {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $LOGNAME skipped (lock held)" >> "$LOG_FILE"
  exit 0
}

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Source the host .env FIRST so the subprocess inherits every bot
# token and PROXY_URL agents rely on. In the pre-6.5 docker-exec path
# the container's environment carried these; the host must explicitly
# export them before the python3 invocation.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# P1.2 bwrap opt-in: only set CLAWFORD_ISOLATION_MODE=bwrap when this
# specific cron's $LOGNAME appears in the per-host allowlist. File-
# based opt-in (per feedback_file_opt_out_pattern.md) so flipping a
# cron in/out of bwrap is one line edit + nothing to redeploy.
# Browser-driven crons stay OFF the allowlist by default — Camoufox/
# Firefox crash under the default profile (SysV shared memory).
BWRAP_ALLOWLIST="${BWRAP_ALLOWLIST_FILE:-/home/openclaw/.clawford/bwrap-allowlist.txt}"
if [[ -f "$BWRAP_ALLOWLIST" ]] && grep -Fxq "$LOGNAME" "$BWRAP_ALLOWLIST"; then
  export CLAWFORD_ISOLATION_MODE=bwrap
fi

# Invoke via contract_wrap.py (P0.3): gives every cron the forensic
# envelope (trace_id auto-injection, agent_id resolution, exit-code
# normalization) AND the bwrap wrapping when the env var is set.
# Pre-2026-04-16 we ran scripts bare; contract_wrap is now the
# canonical path so a SHARED_RUNTIME_MODULES regression (the kind
# that bit activity-email-check after the P0.4 wire-in) is caught
# at envelope-construction time, not at runtime import.
CONTRACT_WRAP="/home/openclaw/repo/agents/shared/contract_wrap.py"
OUTPUT=$(timeout "$TIMEOUT_S" /usr/bin/python3 "$CONTRACT_WRAP" --timeout "$TIMEOUT_S" "$SCRIPT" 2>&1)
EXIT_CODE=$?

{
  echo "[$TS] $LOGNAME exit=$EXIT_CODE"
  echo "$OUTPUT" | tail -5
} >> "$LOG_FILE"

# Extract the LAST line of stdout — per SCRIPT_CONTRACT, the final
# line is a single JSON object. Use Python to parse it safely rather
# than jq (which isn't guaranteed installed on the host).
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
  # Prefer the 'alert' field (SCRIPT_CONTRACT v2 shape, used by
  # heartbeat.py). Fall back to 'message' (v1 shape, used by
  # linkedin-keepalive.py etc.) so older scripts that predate the
  # contract still relay a useful Telegram body.
  ALERT=$(/usr/bin/python3 -c "
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
