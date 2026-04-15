#!/usr/bin/env bash
# retire.sh — Mr Fixit retirement (post-Phase-7 host-cron world).
#
# DO NOT RUN THIS UNLESS PROBATION HAS FAILED.
#
# Probation runs 2026-04-11 → 2026-04-25. If Mr Fixit fails any
# criterion in ~/Dropbox/openclaw-backup/fix-it/probation.md (P1, P2,
# or P3 are one-strike; P4 is two-strike), execute this script to
# retire the agent.
#
# How retirement works in Phase 6.5+:
#
#   1. Add "fix-it" to ~/.clawford/disabled-agents.txt.
#   2. Re-run ops/scripts/install-host-cron.sh. The installer's
#      disabled-agents mechanism skips every fix-it cron on install
#      and evicts any pre-existing fix-it crontab entries in its
#      drift sweep. Retirement is persistent — subsequent installer
#      runs will keep fix-it disabled as long as it appears in the
#      file.
#   3. Mark fix-it.status.md as "retired" in the shared brain.
#
# Scripts under ~/.clawford/fix-it-workspace/scripts/ are preserved so
# Sam can still run them manually from a Claude Code session.
#
# Reversal: remove "fix-it" from ~/.clawford/disabled-agents.txt, then
#           rerun ops/scripts/install-host-cron.sh.

set -euo pipefail

if [ "${1:-}" != "--confirm" ]; then
    cat <<'EOF'
retire.sh — Mr Fixit retirement (post-Phase-7).

This adds fix-it to ~/.clawford/disabled-agents.txt, re-runs
install-host-cron.sh to evict live fix-it crons, and marks
fix-it.status.md as retired. Scripts are preserved for manual use.

Prerequisites:
  - Probation has failed (check ~/Dropbox/openclaw-backup/fix-it/probation.md)
  - Sam has explicitly decided to retire (not extend, not keep)
  - You are running this on the VPS (openclaw@203.0.113.10)

To proceed: bash retire.sh --confirm

Aborting.
EOF
    exit 1
fi

DISABLED_AGENTS_FILE="${DISABLED_AGENTS_FILE:-$HOME/.clawford/disabled-agents.txt}"
INSTALLER="$HOME/repo/ops/scripts/install-host-cron.sh"
BRAIN="$HOME/Dropbox/openclaw-backup"
UTC=$(date -u +"%Y-%m-%d %H:%M UTC")
DATE=$(date -u +%Y-%m-%d)

echo "============================================"
echo "  Mr Fixit — Retirement"
echo "  $UTC"
echo "============================================"
echo ""

# Step 1: add fix-it to the disabled-agents file (idempotent)
echo "Step 1: Adding 'fix-it' to $DISABLED_AGENTS_FILE..."
mkdir -p "$(dirname "$DISABLED_AGENTS_FILE")"
touch "$DISABLED_AGENTS_FILE"
if grep -qE '^[[:space:]]*fix-it[[:space:]]*$' "$DISABLED_AGENTS_FILE"; then
    echo "  already present"
else
    {
        if [ -s "$DISABLED_AGENTS_FILE" ]; then
            cat "$DISABLED_AGENTS_FILE"
        fi
        echo "# Retired via agents/fix-it/retire.sh on $DATE"
        echo "fix-it"
    } > "$DISABLED_AGENTS_FILE.tmp"
    mv "$DISABLED_AGENTS_FILE.tmp" "$DISABLED_AGENTS_FILE"
    echo "  added"
fi
echo ""

# Step 2: re-run install-host-cron.sh so the installer evicts live
# fix-it crons via its disabled-agents sweep
echo "Step 2: Re-running install-host-cron.sh to evict fix-it crons..."
if [ ! -x "$INSTALLER" ] && [ ! -f "$INSTALLER" ]; then
    echo "  ERROR: installer not found at $INSTALLER" >&2
    echo "  Resolve manually: bash \$HOME/repo/ops/scripts/install-host-cron.sh" >&2
    exit 1
fi
bash "$INSTALLER"
echo ""

# Step 3: mark status retired
echo "Step 3: Marking fix-it.status.md as retired..."
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

Mr Fixit was retired on $DATE per probation.md. Fix-it's host-cron
entries were evicted from the VPS crontab by install-host-cron.sh's
disabled-agents sweep. Retirement is persistent: subsequent installer
runs keep fix-it disabled as long as "fix-it" appears in
~/.clawford/disabled-agents.txt.

Scripts are preserved under ~/.clawford/fix-it-workspace/scripts/ for
ad-hoc manual use via Claude Code sessions:
  - diagnose-approval.py
  - security-audit.py
  - heartbeat.py
  - ~/Dropbox/openclaw-backup/scripts/validate.py

The Telegram binding is dormant.

Reversal: remove "fix-it" from ~/.clawford/disabled-agents.txt, then
          bash ~/repo/ops/scripts/install-host-cron.sh
EOF
echo "  written: $BRAIN/agents/fix-it.status.md"
echo ""

echo "============================================"
echo "  Retirement complete."
echo ""
echo "  Verify crons removed:"
echo "    crontab -l | grep fix-it    (should print nothing)"
echo ""
echo "  Verify disabled-agents file:"
echo "    cat $DISABLED_AGENTS_FILE"
echo ""
echo "  Reversal:"
echo "    Remove 'fix-it' from $DISABLED_AGENTS_FILE"
echo "    bash \$HOME/repo/ops/scripts/install-host-cron.sh"
echo "============================================"
