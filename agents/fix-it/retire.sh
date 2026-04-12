#!/usr/bin/env bash
# retire.sh — Mr Fixit retirement script.
#
# DO NOT RUN THIS UNLESS PROBATION HAS FAILED.
#
# Probation runs 2026-04-11 → 2026-04-25. If Mr Fixit fails any criterion in
# ~/Dropbox/openclaw-backup/fix-it/probation.md (P1, P2, or P3 are one-strike;
# P4 is two-strike), execute this script to retire the LLM-driven role and
# replace it with deterministic system-cron entries.
#
# What this does:
#   1. Removes all 11 fix-it OpenClaw crons (LLM-driven)
#   2. Installs host crontab entries that call the existing Python scripts directly
#   3. Marks fix-it status as "retired" in the shared brain
#   4. Leaves diagnose-approval.py / security-audit.py / heartbeat.py in place
#      so Sam can still run them manually from a Claude Code session
#
# What this does NOT do:
#   - Delete the fix-it agent registration (left in place; just dormant)
#   - Touch any other agent
#   - Remove the scripts themselves
#   - Push anything to GitHub (Sam decides commit timing)
#
# Reversal: re-run agents/fix-it/deploy.sh from inside the container.

set -euo pipefail

if [ "${1:-}" != "--confirm" ]; then
    cat <<'EOF'
retire.sh — Mr Fixit retirement.

This is destructive. It will remove all 11 fix-it OpenClaw crons and replace
them with host system-cron entries (no LLM in the loop).

Prerequisites:
  - Probation has failed (check ~/Dropbox/openclaw-backup/fix-it/probation.md)
  - Sam has explicitly decided to retire (not extend, not keep)
  - You are running this from the host (not inside the container)

To proceed: bash retire.sh --confirm

Aborting.
EOF
    exit 1
fi

BRAIN="$HOME/Dropbox/openclaw-backup"
COMPOSE_FILE="$HOME/openclaw/docker-compose.yml"
CRONLOG="$HOME/.openclaw/fix-it-cron.log"
CRONTAB_MARKER="# === fix-it retired ($(date -u +%Y-%m-%d)) ==="

oc() {
    docker compose -f "$COMPOSE_FILE" exec -T openclaw-gateway openclaw "$@"
}

echo "============================================"
echo "  Mr Fixit — Retirement"
echo "  $(date -u +%Y-%m-%d\ %H:%M\ UTC)"
echo "============================================"
echo ""

# Step 1: Remove all fix-it OpenClaw crons
echo "Step 1: Removing OpenClaw fix-it crons..."
for cron_name in heartbeat-check morning-status brain-validation conflict-scan \
                 file-size-monitor monthly-archival security-audit update-check \
                 cron-self-check obsidian-briefing probation-end-reminder; do
    if oc cron remove --agent fix-it --name "$cron_name" 2>/dev/null; then
        echo "  removed: $cron_name"
    else
        echo "  (not present): $cron_name"
    fi
done
echo ""

# Step 2: Install host crontab entries
echo "Step 2: Installing host crontab entries..."

mkdir -p "$(dirname "$CRONLOG")"

DC="docker compose -f $COMPOSE_FILE exec -T openclaw-gateway"

CURRENT=$(crontab -l 2>/dev/null || echo "")
if echo "$CURRENT" | grep -qF "$CRONTAB_MARKER"; then
    echo "  already installed (marker present); skipping"
else
    {
        echo "$CURRENT"
        echo ""
        echo "$CRONTAB_MARKER"
        echo "*/30 * * * * $DC python3 /home/node/.openclaw/fix-it-workspace/scripts/heartbeat.py >> $CRONLOG 2>&1"
        echo "0 */6 * * *  $DC python3 /home/node/Dropbox/openclaw-backup/scripts/validate.py >> $CRONLOG 2>&1"
        echo "0 */2 * * *  find $BRAIN -name '*conflicted copy*' -type f >> $CRONLOG 2>&1"
        echo "0 12 * * *   find $BRAIN -type f -size +500k >> $CRONLOG 2>&1"
        echo "0 4 * * 0    $DC python3 /home/node/.openclaw/fix-it-workspace/scripts/security-audit.py >> $CRONLOG 2>&1"
        echo "10 12 * * *  $DC python3 /home/node/Dropbox/openclaw-backup/scripts/obsidian-briefing/generate.py >> $CRONLOG 2>&1"
        echo "# end fix-it retired"
    } | crontab -
    echo "  installed retired-fix-it cron block"
fi
echo ""

# Step 3: Mark status retired
echo "Step 3: Marking fix-it.status.md as retired..."
cat > "$BRAIN/agents/fix-it.status.md" <<EOF
# Fix-It — Status

- **last_heartbeat:** $(date -u +"%Y-%m-%d %H:%M UTC")
- **status:** retired
- **last_cron_run:** retired via retire.sh on $(date -u +%Y-%m-%d)
- **last_cron_result:** Probation failed. LLM role retired. Host cron handles monitoring.
- **error_log:** none
- **token_usage_today:** 0

## Retirement note

Mr Fixit's LLM-driven crons were replaced with host system-cron entries on $(date -u +%Y-%m-%d).
The agent registration is preserved but dormant — the agent will not respond to Telegram.
For ad-hoc diagnosis, Sam uses Claude Code sessions and the surviving scripts:
  - ~/.openclaw/fix-it-workspace/scripts/diagnose-approval.py
  - ~/.openclaw/fix-it-workspace/scripts/security-audit.py
  - ~/.openclaw/fix-it-workspace/scripts/heartbeat.py
  - ~/Dropbox/openclaw-backup/scripts/validate.py

Reversal: bash agents/fix-it/deploy.sh from inside the container.
EOF
echo "  written: $BRAIN/agents/fix-it.status.md"
echo ""

echo "============================================"
echo "  Retirement complete."
echo ""
echo "  Verify host crontab:"
echo "    crontab -l | grep -A 10 'fix-it retired'"
echo ""
echo "  Verify OpenClaw crons removed:"
echo "    docker compose -f $COMPOSE_FILE exec -T openclaw-gateway openclaw cron list | grep fix-it"
echo "    (should print nothing)"
echo ""
echo "  Reversal command:"
echo "    bash $HOME/repo/agents/fix-it/deploy.sh"
echo "============================================"
