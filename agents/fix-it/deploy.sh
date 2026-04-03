#!/usr/bin/env bash
# Deploy Mr Fixit — Run this AFTER `openclaw agents add fix-it`
# Usage: bash /tmp/deploy-fixit.sh
#
# Prerequisites:
#   - Docker container running: cd ~/openclaw && docker compose up -d
#   - openclaw agents add fix-it (interactive onboarding completed inside container)
#   - Device pairing approved
#   - SOUL.md, IDENTITY.md, TOOLS.md in /tmp/
#   - .env with TELEGRAM_CHAT_ID and FIXIT_BOT_TOKEN in /tmp/ or ~/openclaw/

set -euo pipefail

# Load secrets from .env
if [ -f /tmp/.env ]; then
    source /tmp/.env
elif [ -f ~/openclaw/.env ]; then
    source ~/openclaw/.env
elif [ -f .env ]; then
    source .env
fi

BRAIN="$HOME/Dropbox/openclaw-backup"
WORKSPACE="$HOME/.openclaw/fix-it-workspace"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID in .env}"
FIXIT_BOT_TOKEN="${FIXIT_BOT_TOKEN:?Set FIXIT_BOT_TOKEN in .env}"
TELEGRAM_ACCOUNT="fixit"
COMPOSE_FILE="$HOME/openclaw/docker-compose.yml"

# OpenClaw CLI wrapper — runs through Docker
oc() {
    docker compose -f "$COMPOSE_FILE" exec -T openclaw-gateway openclaw "$@"
}

echo "============================================"
echo "  Mr Fixit — Deployment Script"
echo "  OpenClaw 2026.4.1 (Docker)"
echo "============================================"
echo ""

# ── Step 1: Install Configuration Files ──────────────────────

echo "Step 1: Installing config files..."

if [ ! -d "$WORKSPACE" ]; then
    echo "  Creating workspace: $WORKSPACE"
    mkdir -p "$WORKSPACE"
fi

for file in SOUL.md IDENTITY.md TOOLS.md; do
    if [ -f "/tmp/$file" ]; then
        cp "/tmp/$file" "$WORKSPACE/$file"
        echo "  Copied $file -> $WORKSPACE/$file"
    else
        echo "  WARNING: /tmp/$file not found — skipping"
    fi
done

echo ""

# ── Step 2: Create Status File ───────────────────────────────

echo "Step 2: Initializing status file..."

cat > "$BRAIN/agents/fix-it.status.md" << 'EOF'
# Fix-It — Status

- **last_heartbeat:** —
- **status:** initializing
- **last_cron_run:** —
- **last_cron_result:** —
- **error_log:** none
- **token_usage_today:** 0
EOF

echo "  Written: $BRAIN/agents/fix-it.status.md"
echo ""

# ── Step 3: Configure Telegram Channel + Binding ─────────────

echo "Step 3: Configuring Telegram channel + binding..."

oc channels add --channel telegram \
  --token "$FIXIT_BOT_TOKEN" \
  --account "$TELEGRAM_ACCOUNT" \
  --name "Mr Fixit" 2>/dev/null || true
echo "  Telegram account '$TELEGRAM_ACCOUNT' configured"

oc agents bind --agent fix-it --bind "telegram:$TELEGRAM_ACCOUNT" 2>/dev/null || true
echo "  Agent fix-it bound to telegram:$TELEGRAM_ACCOUNT"

echo ""
echo "  NOTE: You must /start the Mr Fixit bot on Telegram and approve pairing:"
echo "  docker compose -f ~/openclaw/docker-compose.yml exec openclaw-gateway openclaw pairing approve telegram <CODE>"
echo ""

# ── Step 4: Set Up Exec Approvals ────────────────────────────

echo "Step 4: Setting exec approvals..."

oc approvals allowlist add --agent fix-it "/usr/bin/*"
echo "  Added /usr/bin/* to allowlist"

oc approvals allowlist add --agent fix-it "/bin/*"
echo "  Added /bin/* to allowlist"

oc approvals allowlist add --agent fix-it "/usr/local/bin/*"
echo "  Added /usr/local/bin/* to allowlist"

echo ""

# ── Step 5: Register Crons ───────────────────────────────────

echo "Step 5: Registering 9 crons..."

# 1. Heartbeat — every 30 minutes (SILENT on all-clear)
oc cron add \
  --agent fix-it \
  --name "heartbeat-check" \
  --cron "*/30 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Read all agent status files in ~/Dropbox/openclaw-backup/agents/. For each agent, check if last_heartbeat is older than 90 minutes. Update your own status file with current heartbeat. If ALL agents are healthy: update the status file silently and produce NO output — do not send any message. If any agent is unhealthy or down: send me a Telegram message describing which agent is unhealthy and what you found."
echo "  [1/9] heartbeat-check (silent on all-clear)"

# 2. Morning status — daily at 06:00 UTC
oc cron add \
  --agent fix-it \
  --name "morning-status" \
  --cron "0 6 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Compile a morning status report. Read all agent status files, run python3 ~/Dropbox/openclaw-backup/scripts/validate.py, check for Dropbox conflicts with find ~/Dropbox/openclaw-backup/ -name '*conflicted copy*' -type f, and note any open alerts. Send the full report to me on Telegram. Use the format from CRONS.md."
echo "  [2/9] morning-status"

# 3. Brain validation — every 6 hours (SILENT on pass)
oc cron add \
  --agent fix-it \
  --name "brain-validation" \
  --cron "0 */6 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: python3 ~/Dropbox/openclaw-backup/scripts/validate.py. If validation PASSES: update your status file silently and produce NO output. If validation FAILS: send me a Telegram message with the specific failures immediately."
echo "  [3/9] brain-validation (silent on pass)"

# 4. Dropbox conflict scan — every 2 hours (SILENT on clean)
oc cron add \
  --agent fix-it \
  --name "conflict-scan" \
  --cron "0 */2 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: find ~/Dropbox/openclaw-backup/ -name '*conflicted copy*' -type f. If NO conflicts found: update your status file silently and produce NO output. If conflicts ARE found: send me a Telegram message with the filenames. Do NOT attempt to merge."
echo "  [4/9] conflict-scan (silent on clean)"

# 5. File size monitor — daily at 12:00 UTC (SILENT on clean)
oc cron add \
  --agent fix-it \
  --name "file-size-monitor" \
  --cron "0 12 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: find ~/Dropbox/openclaw-backup/ -type f -size +500k. If NO large files found: update your status file silently and produce NO output. If large files ARE found: send me a Telegram message with the filenames and sizes."
echo "  [5/9] file-size-monitor (silent on clean)"

# 6. Monthly archival — 1st of each month at 03:00 UTC
oc cron add \
  --agent fix-it \
  --name "monthly-archival" \
  --cron "0 3 1 * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Run monthly archival. Create ~/Dropbox/openclaw-backup/archive/YYYY-MM/ for current month. Scan ALL files matching ~/Dropbox/openclaw-backup/facts/*.md (every monthly file, not just the current month). For each fact entry, calculate effective_confidence = original_confidence * 0.5^(days_since_recorded / half_life) using category half-lives: identity=never, established=365, situation=90, preference=180, plan=30, logistics=7, rumor=14. Move facts with effective_confidence < 0.2 AND recorded > 90 days ago to the archive. Also scan tasks/queue.md and move tasks with status done and completed > 90 days ago. Write an archive manifest listing everything moved. Report results on Telegram."
echo "  [6/9] monthly-archival"

# 7. Security audit — weekly Sunday 04:00 UTC
oc cron add \
  --agent fix-it \
  --name "security-audit" \
  --cron "0 4 * * 0" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Run: openclaw security audit --deep. Report results on Telegram. If issues are found, list them with severity. Do NOT run --fix automatically. Wait for my confirmation."
echo "  [7/9] security-audit"

# 8. Update check — weekly Wednesday 04:00 UTC
oc cron add \
  --agent fix-it \
  --name "update-check" \
  --cron "0 4 * * 3" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Run: openclaw update. Report current version and whether an update is available on Telegram. Do NOT apply updates automatically. Wait for my confirmation."
echo "  [8/9] update-check"

# 9. Self-check — daily at midnight UTC (SILENT on pass)
oc cron add \
  --agent fix-it \
  --name "cron-self-check" \
  --cron "0 0 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Run: openclaw cron list. Verify all 9 fix-it crons are registered (heartbeat-check, morning-status, brain-validation, conflict-scan, file-size-monitor, monthly-archival, security-audit, update-check, cron-self-check). Filter the output visually for fix-it entries. If all 9 are present: update your status file silently and produce NO output. If any are missing: attempt to re-register them and send me a Telegram message."
echo "  [9/9] cron-self-check (silent on pass)"

echo ""

# ── Step 6: Security Hardening ───────────────────────────────

echo "Step 6: Security hardening..."

sudo chattr +i "$WORKSPACE/SOUL.md" 2>/dev/null && echo "  SOUL.md locked (immutable)" || echo "  WARNING: Could not lock SOUL.md (run: sudo chattr +i $WORKSPACE/SOUL.md)"
sudo chattr +i "$WORKSPACE/IDENTITY.md" 2>/dev/null && echo "  IDENTITY.md locked (immutable)" || echo "  WARNING: Could not lock IDENTITY.md (run: sudo chattr +i $WORKSPACE/IDENTITY.md)"

echo ""

# ── Verify ───────────────────────────────────────────────────

echo "============================================"
echo "  Verification"
echo "============================================"
echo ""

echo "Agent list:"
oc agents list
echo ""

echo "Crons registered:"
oc cron list
echo ""

echo "Exec approvals:"
oc approvals get
echo ""

echo "Status file:"
cat "$BRAIN/agents/fix-it.status.md"
echo ""

echo "Workspace:"
ls -la "$WORKSPACE/"
echo ""

echo "Immutable files:"
lsattr "$WORKSPACE/SOUL.md" "$WORKSPACE/IDENTITY.md" 2>/dev/null || echo "  (lsattr not available)"
echo ""

echo "============================================"
echo "  Deployment complete!"
echo ""
echo "  Smoke test (use IDs from cron list above):"
echo "  oc() { docker compose -f ~/openclaw/docker-compose.yml exec -T openclaw-gateway openclaw \"\$@\"; }"
echo "  oc cron run <heartbeat-check-id>"
echo "  oc cron run <brain-validation-id>"
echo "  oc cron run <morning-status-id>"
echo "  Send Telegram: 'Status check — report all systems.'"
echo ""
echo "  Run test suite:"
echo "  bash ~/openclaw-tests/test-agent.sh fix-it"
echo "============================================"
