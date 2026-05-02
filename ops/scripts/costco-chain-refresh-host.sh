#!/usr/bin/env bash
# costco-chain-refresh-host.sh — proactive 22h Costco chain refresh.
#
# WHY THIS EXISTS
# ---------------
# The public-client refresh_token chain has a ~24h absolute lifetime
# (B2C SPA registration). Headless `/token` rotation works inside the
# window but cannot extend past it — at the 24h mark, B2C starts
# issuing RTs with `window=22s` lifetimes (cap-tail behavior) and
# headless dies with AADB2C90080 / class=rt_stale.
#
# Pre-2026-05-02 the chain was refreshed implicitly as a side effect
# of the safety-net residential silent path, which fires DataImpulse-
# only and was unreliable when DataImpulse + Camoufox misbehaved
# (page-closed errors, "Failed to get IP address", or B2C 429 cycles
# on the credential fallback).
#
# This wrapper runs `costco-token-daemon.py --chain-refresh` every
# 22 hours via the on-demand SOCKS tunnel:
#   1. _ensure_tunnel_up (laptop reachable check)
#   2. spawn_step("residential", extra_env=tunnel) — fresh SSO cookies
#   3. _chain_pkce_bootstrap (env=tunnel) — fresh public-client RT
#   4. _ensure_tunnel_down — back to break-glass posture
#
# 22h cadence stays safely under the 24h chain TTL; tunnel is up for
# ~30 s per cycle and otherwise stays down. No credential submission
# (so no B2C /SelfAsserted 429 risk).
#
# INSTALL: ops/scripts/install-host-cron.sh (idempotent)
# LOG:     ~/.clawford/logs/costco-chain-refresh-host.log (rotated @ 1 MB)
set -u

DAEMON="/home/openclaw/.clawford/shopping-workspace/scripts/costco-token-daemon.py"
LOG_FILE="/home/openclaw/.clawford/logs/costco-chain-refresh-host.log"
LOCK_FILE="/tmp/costco-chain-refresh-host.lock"
ENV_FILE="/home/openclaw/clawford/.env"

mkdir -p "$(dirname "$LOG_FILE")"

exec 200>"$LOCK_FILE"
flock -n 200 || {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] skipped (previous run still holds lock)" >> "$LOG_FILE"
  exit 0
}

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# 300s ceiling: residential silent ~30s + PKCE bootstrap ~30s +
# tunnel up/down + Camoufox launch overhead. Generous; flock -n on
# the next 22h tick still prevents overlap.
OUTPUT=$(timeout 300 /usr/bin/python3 "$DAEMON" --chain-refresh 2>&1)
EXIT_CODE=$?

{
  echo "[$TS] exit=$EXIT_CODE"
  echo "$OUTPUT" | tail -8
} >> "$LOG_FILE"

# Log rotation at 1 MB.
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

# Always exit 0 — the daemon's chain_refresh_main already gates the
# Telegram nudge on the 24h cooldown, so cron MAILTO spam on a
# transient failure is unnecessary.
exit 0
