#!/usr/bin/env bash
# Deploy Huckle Cat (Connector) — Run this AFTER `openclaw agents add connector`
# Usage: bash /tmp/deploy-connector.sh
#
# Prerequisites:
#   - Docker container running: cd ~/openclaw && docker compose up -d
#   - openclaw agents add connector (interactive onboarding completed inside container)
#   - Device pairing approved
#   - SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md, MEMORY.md in /tmp/
#   - scripts/ directory with all Python scripts in /tmp/
#   - .env with TELEGRAM_CHAT_ID, CONNECTOR_BOT_TOKEN in /tmp/ or ~/openclaw/

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
WORKSPACE="$HOME/.openclaw/connector-workspace"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID in .env}"
CONNECTOR_BOT_TOKEN="${CONNECTOR_BOT_TOKEN:?Set CONNECTOR_BOT_TOKEN in .env}"
TELEGRAM_ACCOUNT="huckle"
COMPOSE_FILE="$HOME/openclaw/docker-compose.yml"

# OpenClaw CLI wrapper — runs through Docker
oc() {
    docker compose -f "$COMPOSE_FILE" exec -T openclaw-gateway openclaw "$@"
}

echo "============================================"
echo "  Huckle Cat (Connector) — Deployment"
echo "  OpenClaw 2026.4.1 (Docker)"
echo "============================================"
echo ""

# ── Step 1: Install Configuration Files ──────────────────────

echo "Step 1: Installing config files..."

mkdir -p "$WORKSPACE"
mkdir -p "$WORKSPACE/scripts"
mkdir -p "$WORKSPACE/cache"
mkdir -p "$WORKSPACE/logs"

for file in SOUL.md IDENTITY.md TOOLS.md AGENTS.md USER.md HEARTBEAT.md MEMORY.md; do
    if [ -f "/tmp/$file" ]; then
        cp "/tmp/$file" "$WORKSPACE/$file"
        echo "  Copied $file -> $WORKSPACE/$file"
    else
        echo "  WARNING: /tmp/$file not found — skipping"
    fi
done

# Copy Python scripts
for script in people-scan.py notes-triage.py commitment-scan.py timed-deliver.py; do
    if [ -f "/tmp/scripts/$script" ]; then
        cp "/tmp/scripts/$script" "$WORKSPACE/scripts/$script"
        echo "  Copied scripts/$script -> $WORKSPACE/scripts/$script"
    else
        echo "  WARNING: /tmp/scripts/$script not found — skipping"
    fi
done

# Copy connector config
if [ -f "/tmp/connector-config.json" ]; then
    cp "/tmp/connector-config.json" "$WORKSPACE/connector-config.json"
    echo "  Copied connector-config.json -> $WORKSPACE/connector-config.json"
fi

echo ""

# ── Step 2: Create Status File ───────────────────────────────

echo "Step 2: Initializing status file..."

cat > "$BRAIN/agents/connector.status.md" << 'EOF'
# Connector — Status

- **last_heartbeat:** —
- **status:** initializing
- **last_cron_run:** —
- **last_cron_result:** —
- **people_tracked:** 0
- **overdue_count:** 0
- **error_log:** none
- **token_usage_today:** 0
EOF

echo "  Written: $BRAIN/agents/connector.status.md"
echo ""

# ── Step 3: Seed State Files ────────────────────────────────

echo "Step 3: Seeding state files..."

# Seed empty pending-triage
cat > "$WORKSPACE/pending-triage.json" << 'EOF'
{
  "pending": {},
  "last_pruned": null
}
EOF
echo "  Written: $WORKSPACE/pending-triage.json"

# Seed empty checkin-log
cat > "$WORKSPACE/checkin-log.json" << 'EOF'
{
  "checkins": []
}
EOF
echo "  Written: $WORKSPACE/checkin-log.json"

# Seed connector config if not already copied
if [ ! -f "$WORKSPACE/connector-config.json" ]; then
    cat > "$WORKSPACE/connector-config.json" << 'EOF'
{
  "cadences": {
    "family-inner": {"check_days": 1, "nudge": false, "note": "Monitored by Mistress Mouse"},
    "family-extended": {"check_days": 21, "nudge": true},
    "friends-close": {"check_days": 30, "nudge": true},
    "professional-inner": {"check_days": 7, "nudge": true},
    "professional-outer": {"check_days": 90, "nudge": true},
    "holiday-card": {"check_days": 365, "nudge": false, "note": "Life events only"}
  },
  "timezone": "America/Los_Angeles",
  "nudge": {
    "max_per_day": 5,
    "skip_circles": ["family-inner", "holiday-card"]
  },
  "triage": {
    "max_batch_size": 10,
    "categories": ["fact", "commitment", "task", "shopping", "unclear"]
  }
}
EOF
    echo "  Written: $WORKSPACE/connector-config.json"
fi

echo ""

# ── Step 4: Configure Telegram Channel + Binding ─────────────

echo "Step 4: Configuring Telegram channel + binding..."

oc channels add --channel telegram \
  --token "$CONNECTOR_BOT_TOKEN" \
  --account "$TELEGRAM_ACCOUNT" \
  --name "Huckle Cat" 2>/dev/null || true
echo "  Telegram account '$TELEGRAM_ACCOUNT' configured"

oc agents bind --agent connector --bind "telegram:$TELEGRAM_ACCOUNT" 2>/dev/null || true
echo "  Agent connector bound to telegram:$TELEGRAM_ACCOUNT"

echo ""
echo "  NOTE: You must /start @openclaw_huckle_cat_bot on Telegram and approve pairing:"
echo "  docker compose -f ~/openclaw/docker-compose.yml exec openclaw-gateway openclaw pairing approve telegram <CODE>"
echo ""

# ── Step 5: Set Up Exec Approvals ────────────────────────────

echo "Step 5: Setting exec approvals..."

oc approvals allowlist add --agent connector "/usr/bin/*"
echo "  Added /usr/bin/* to allowlist"

oc approvals allowlist add --agent connector "/bin/*"
echo "  Added /bin/* to allowlist"

oc approvals allowlist add --agent connector "/usr/local/bin/*"
echo "  Added /usr/local/bin/* to allowlist"

oc approvals allowlist add --agent connector "python3 ~/.openclaw/connector-workspace/scripts/*"
echo "  Added python3 scripts/* to allowlist"

oc approvals allowlist add --agent connector "python3 -"
echo "  Added python3 stdin to allowlist"

# ── Exec policy: trusted local automation ──
# Without this, crons fail with "exec denied: Cron runs cannot wait for
# interactive exec approval." The LLM generates compound shell commands
# (redirects, pipes, heredocs) that don't match simple allowlist patterns.
# For a private VPS running trusted agents, security=full + ask=off is the
# right posture — no human approval needed for exec calls.
oc config set tools.exec.security full
oc config set tools.exec.ask off
echo "  Set tools.exec.security=full, ask=off"

echo ""

# ── Step 6: Register Crons ───────────────────────────────────

echo "Step 6: Registering 4 crons..."

# 1. Morning relationship nudge — daily at 11:55 UTC (4:55 AM PT), timed delivery at 12:00 UTC (5:00 AM PT)
oc cron add \
  --agent connector \
  --name "morning-relationship-nudge" \
  --cron "55 11 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --message "Generate and deliver the morning relationship nudge. 1) Run: python3 ~/.openclaw/connector-workspace/scripts/people-scan.py. Read the JSON output — it contains overdue, approaching, and healthy contacts with their check-in status. 2) Format the nudge using the template in CRONS.md. Group by overdue then approaching. Include each person's name, relationship, days since last contact, and preferred channel. 3) If no overdue or approaching: use the 'everyone accounted for' template. 4) Write formatted output to cache/morning-nudge.txt. 5) Run: python3 ~/.openclaw/connector-workspace/scripts/timed-deliver.py cache/morning-nudge.txt --token-env CONNECTOR_BOT_TOKEN. 6) Update your status file."
echo "  [1/4] morning-relationship-nudge (daily 11:55 UTC, deliver at 12:00 UTC / 5:00 AM PT)"

# 2. Notes triage — twice daily (SILENT when inbox is empty)
oc cron add \
  --agent connector \
  --name "notes-triage" \
  --cron "0 8,20 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Triage untriaged notes from the inbox. Run: python3 ~/.openclaw/connector-workspace/scripts/notes-triage.py. Read the JSON output. If no untriaged notes: produce NO output. If untriaged notes exist: read pending-triage.json to skip already-presented notes. For each new note, categorize it (fact/commitment/task/shopping/unclear). Present up to 10 on Telegram with categories and '/confirm to save · /dismiss N to skip item N'. Record presented note IDs in pending-triage.json."
echo "  [2/4] notes-triage (twice daily, silent when inbox empty)"

# 3. Heartbeat — every 30 minutes (SILENT on success)
oc cron add \
  --agent connector \
  --name "heartbeat" \
  --cron "*/30 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Update your heartbeat. Write the current UTC timestamp to last_heartbeat in ~/Dropbox/openclaw-backup/agents/connector.status.md. Verify these files exist: connector-config.json, pending-triage.json. Check cache/ for files older than 14 days — prune if found. Prune pending-triage.json entries older than 48 hours. Produce NO output if everything is normal."
echo "  [3/4] heartbeat (silent on success)"

# 4. Weekly relationship review — Sunday at 00:00 UTC (5:00 PM PT Saturday)
oc cron add \
  --agent connector \
  --name "weekly-review" \
  --cron "0 0 * * 0" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Generate the weekly relationship review. 1) Run people-scan.py to get current check-in state. 2) Read checkin-log.json for this week's check-ins. 3) Compile: check-ins recorded, notes triaged, facts added, people tracked, still-overdue contacts. 4) Format using the template in CRONS.md. 5) Deliver to Telegram."
echo "  [4/4] weekly-review (Sunday 00:00 UTC)"

echo ""

# ── Step 7: Security Hardening ───────────────────────────────

echo "Step 7: Security hardening..."

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
cat "$BRAIN/agents/connector.status.md"
echo ""

echo "Workspace:"
ls -la "$WORKSPACE/"
echo ""

echo "Scripts:"
ls -la "$WORKSPACE/scripts/"
echo ""

echo "Config:"
cat "$WORKSPACE/connector-config.json"
echo ""

echo "State files:"
cat "$WORKSPACE/pending-triage.json"
echo ""
cat "$WORKSPACE/checkin-log.json"
echo ""

echo "Immutable files:"
lsattr "$WORKSPACE/SOUL.md" "$WORKSPACE/IDENTITY.md" 2>/dev/null || echo "  (lsattr not available)"
echo ""

echo "============================================"
echo "  Deployment complete!"
echo ""
echo "  Next steps:"
echo "  1. /start @openclaw_huckle_cat_bot on Telegram"
echo "  2. Approve pairing: oc pairing approve telegram <CODE>"
echo "  3. Smoke test: oc cron run <morning-relationship-nudge-id>"
echo "  4. Test on-demand: send '/nudge' on Telegram"
echo ""
echo "  Run test suite:"
echo "  bash ~/openclaw-tests/test-agent.sh connector"
echo "============================================"
