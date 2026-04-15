#!/usr/bin/env bash
# retire.sh — Mr Fixit retirement (post-Phase-7 host-cron world).
#
# DO NOT RUN THIS UNLESS PROBATION HAS FAILED.
#
# Probation runs 2026-04-11 → 2026-04-25. If Mr Fixit fails any criterion
# in ~/Dropbox/openclaw-backup/fix-it/probation.md (P1, P2, or P3 are
# one-strike; P4 is two-strike), execute this script to retire the agent.
#
# Phase 6.5 made fix-it's cron layer fully host-native — there is no
# LLM-driven layer left to remove. Retirement now means:
#
#   1. Remove fix-it's host-cron entries from the VPS crontab
#   2. Mark fix-it.status.md as "retired" in the shared brain
#
# Scripts under ~/.openclaw/fix-it-workspace/scripts/ are preserved so
# Sam can still run them manually from a Claude Code session.
#
# WARNING: re-running ops/scripts/install-host-cron.sh AFTER retirement
# will RESTORE the fix-it cron entries. Making retirement persist across
# installer runs would require a disabled-agents mechanism in
# install-host-cron.sh — that is not yet implemented. For now, retirement
# is one-way until the next install-host-cron.sh run.
#
# Reversal: bash ~/repo/ops/scripts/install-host-cron.sh

set -euo pipefail

if [ "${1:-}" != "--confirm" ]; then
    cat <<'EOF'
retire.sh — Mr Fixit retirement (post-Phase-7).

This removes fix-it's host-cron entries and marks fix-it.status.md as
retired. Scripts are preserved for manual use.

Prerequisites:
  - Probation has failed (check ~/Dropbox/openclaw-backup/fix-it/probation.md)
  - Sam has explicitly decided to retire (not extend, not keep)
  - You are running this on the VPS (openclaw@203.0.113.10)

To proceed: bash retire.sh --confirm

Aborting.
EOF
    exit 1
fi

BRAIN="$HOME/Dropbox/openclaw-backup"
UTC=$(date -u +"%Y-%m-%d %H:%M UTC")
DATE=$(date -u +%Y-%m-%d)
MARKER="# === fix-it retired ($DATE) ==="

echo "============================================"
echo "  Mr Fixit — Retirement"
echo "  $UTC"
echo "============================================"
echo ""

# Step 1: remove fix-it entries from crontab
echo "Step 1: Removing fix-it host-cron entries..."
CURRENT=$(crontab -l 2>/dev/null || echo "")
if [ -z "$CURRENT" ]; then
    echo "  (no crontab; nothing to remove)"
elif echo "$CURRENT" | grep -qF "$MARKER"; then
    echo "  already retired (marker present); skipping"
else
    FILTERED=$(echo "$CURRENT" | grep -v -E '# (fix-it-|script-contract-fix-it-)' || true)
    if [ "$FILTERED" = "$CURRENT" ]; then
        echo "  (no fix-it entries found in crontab)"
    else
        REMOVED=$(echo "$CURRENT" | grep -c -E '# (fix-it-|script-contract-fix-it-)' || true)
        {
            echo "$FILTERED"
            echo ""
            echo "$MARKER"
            echo "# retire.sh removed $REMOVED fix-it host crons on $UTC."
            echo "# DO NOT re-run install-host-cron.sh for fix-it — it will restore these."
        } | crontab -
        echo "  removed $REMOVED fix-it cron entries"
    fi
fi
echo ""

# Step 2: mark status retired
echo "Step 2: Marking fix-it.status.md as retired..."
mkdir -p "$BRAIN/agents"
cat > "$BRAIN/agents/fix-it.status.md" <<EOF
# Fix-It — Status

- **last_heartbeat:** $UTC
- **status:** retired
- **last_cron_run:** retired via retire.sh on $DATE
- **last_cron_result:** Probation failed. Host-cron role retired.
- **error_log:** none
- **token_usage_today:** 0

## Retirement note

Mr Fixit was retired on $DATE per probation.md. Host-cron entries were
removed from the VPS crontab on this date. Scripts are preserved under
~/.openclaw/fix-it-workspace/scripts/ for ad-hoc manual use via Claude
Code sessions:
  - diagnose-approval.py
  - security-audit.py
  - heartbeat.py
  - ~/Dropbox/openclaw-backup/scripts/validate.py

The Telegram binding is dormant.

Reversal: bash ~/repo/ops/scripts/install-host-cron.sh
EOF
echo "  written: $BRAIN/agents/fix-it.status.md"
echo ""

echo "============================================"
echo "  Retirement complete."
echo ""
echo "  Verify crons removed:"
echo "    crontab -l | grep fix-it    (should print nothing except the marker)"
echo ""
echo "  WARNING: re-running install-host-cron.sh will restore fix-it crons."
echo "  Until a disabled-agents mechanism lands, retirement is one-way."
echo ""
echo "  Reversal:"
echo "    bash ~/repo/ops/scripts/install-host-cron.sh"
echo "============================================"
