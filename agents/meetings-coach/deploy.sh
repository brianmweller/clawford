#!/usr/bin/env bash
# Deploy Sergeant Murphy (Meetings Coach) — Run this AFTER `openclaw agents add meetings-coach`
# Usage: bash /tmp/deploy-meetings-coach.sh
#
# Prerequisites:
#   - Docker container running: cd ~/openclaw && docker compose up -d
#   - openclaw agents add meetings-coach (interactive onboarding completed inside container)
#   - Device pairing approved
#   - SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md, MEMORY.md in /tmp/
#   - scripts/ directory with all Python scripts in /tmp/
#   - .env with TELEGRAM_CHAT_ID, MEETINGS_BOT_TOKEN, WORKFLOWY_API_KEY, OPENAI_API_KEY in /tmp/ or ~/openclaw/
#   - google-api-python-client, google-auth-httplib2, google-auth-oauthlib, openai installed in Docker image

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
WORKSPACE="$HOME/.openclaw/meetings-coach-workspace"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID in .env}"
MEETINGS_BOT_TOKEN="${MEETINGS_BOT_TOKEN:?Set MEETINGS_BOT_TOKEN in .env}"
TELEGRAM_ACCOUNT="murphy"
COMPOSE_FILE="$HOME/openclaw/docker-compose.yml"

# OpenClaw CLI wrapper — runs through Docker
oc() {
    docker compose -f "$COMPOSE_FILE" exec -T openclaw-gateway openclaw "$@"
}

echo "============================================"
echo "  Sergeant Murphy (Meetings Coach) — Deployment"
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
for script in gcal-fetch.py gcal-auth.py meeting-prep.py workflowy-sync.py transcript-scan.py commitment-tracker.py person-bootstrap.py timed-deliver.py; do
    if [ -f "/tmp/scripts/$script" ]; then
        cp "/tmp/scripts/$script" "$WORKSPACE/scripts/$script"
        echo "  Copied scripts/$script -> $WORKSPACE/scripts/$script"
    else
        echo "  WARNING: /tmp/scripts/$script not found — skipping"
    fi
done

# Copy meeting config
if [ -f "/tmp/meeting-config.json" ]; then
    cp "/tmp/meeting-config.json" "$WORKSPACE/meeting-config.json"
    echo "  Copied meeting-config.json -> $WORKSPACE/meeting-config.json"
fi

echo ""

# ── Step 2: Create Status File ───────────────────────────────

echo "Step 2: Initializing status file..."

cat > "$BRAIN/agents/meetings-coach.status.md" << 'EOF'
# Meetings Coach — Status

- **last_heartbeat:** —
- **status:** initializing
- **last_cron_run:** —
- **last_cron_result:** —
- **google_auth:** unknown
- **workflowy_auth:** unknown
- **krisp_auth:** unknown
- **error_log:** none
- **token_usage_today:** 0
EOF

echo "  Written: $BRAIN/agents/meetings-coach.status.md"
echo ""

# ── Step 3: Seed State Files ────────────────────────────────

echo "Step 3: Seeding state files..."

# Seed empty sent-alerts
cat > "$WORKSPACE/sent-alerts.json" << 'EOF'
{
  "alerts": {},
  "last_pruned": null
}
EOF
echo "  Written: $WORKSPACE/sent-alerts.json"

# Seed empty processed-transcripts
cat > "$WORKSPACE/processed-transcripts.json" << 'EOF'
{
  "processed": []
}
EOF
echo "  Written: $WORKSPACE/processed-transcripts.json"

# Seed empty workflowy-links
cat > "$WORKSPACE/cache/workflowy-links.json" << 'EOF'
{}
EOF
echo "  Written: $WORKSPACE/cache/workflowy-links.json"

# Seed meeting config if not already copied
if [ ! -f "$WORKSPACE/meeting-config.json" ]; then
    cat > "$WORKSPACE/meeting-config.json" << 'EOF'
{
  "calendars": [
    {"id": "sam.smith@example.com", "label": "Sam (Work)", "emoji": "👨"}
  ],
  "timezone": "America/Los_Angeles",
  "meeting_filters": {
    "skip_titles": ["Focus Time", "Lunch", "Block"],
    "require_attendees_or_video": true
  },
  "workflowy": {
    "enabled": true,
    "ai_bullet_prefix": "[AI]",
    "node_template": ["Pre / during", "📔 Agenda", "📝 Notes", "Post-meeting", "✅ Takeaways"]
  },
  "krisp": {
    "enabled": true,
    "mcp_url": "https://mcp.krisp.ai/mcp"
  }
}
EOF
    echo "  Written: $WORKSPACE/meeting-config.json"
fi

echo ""

# ── Step 4: Configure Telegram Channel + Binding ─────────────

echo "Step 4: Configuring Telegram channel + binding..."

oc channels add --channel telegram \
  --token "$MEETINGS_BOT_TOKEN" \
  --account "$TELEGRAM_ACCOUNT" \
  --name "Sergeant Murphy" 2>/dev/null || true
echo "  Telegram account '$TELEGRAM_ACCOUNT' configured"

oc agents bind --agent meetings-coach --bind "telegram:$TELEGRAM_ACCOUNT" 2>/dev/null || true
echo "  Agent meetings-coach bound to telegram:$TELEGRAM_ACCOUNT"

echo ""
echo "  NOTE: You must /start @openclaw_sergeant_murphy_bot on Telegram and approve pairing:"
echo "  docker compose -f ~/openclaw/docker-compose.yml exec openclaw-gateway openclaw pairing approve telegram <CODE>"
echo ""

# ── Step 5: Set Up Exec Approvals ────────────────────────────

echo "Step 5: Setting exec approvals..."

oc approvals allowlist add --agent meetings-coach "/usr/bin/*"
echo "  Added /usr/bin/* to allowlist"

oc approvals allowlist add --agent meetings-coach "/bin/*"
echo "  Added /bin/* to allowlist"

oc approvals allowlist add --agent meetings-coach "/usr/local/bin/*"
echo "  Added /usr/local/bin/* to allowlist"

echo ""

# ── Step 6: Register Crons ───────────────────────────────────

echo "Step 6: Registering 6 crons..."

# 1. Morning meeting brief — daily at 11:55 UTC (4:55 AM PT), timed delivery at 12:00 UTC (5:00 AM PT)
oc cron add \
  --agent meetings-coach \
  --name "morning-meeting-brief" \
  --cron "55 11 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --message "Generate and deliver the morning meeting brief. 1) Run: python3 ~/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py --days 2. Read the JSON output — these are today's and tomorrow's professional meetings. 2) Filter to real meetings only (is_real_meeting=true). 3) For each real meeting: a) Run person-bootstrap.py to create person files for new attendees. b) Run workflowy-sync.py --create-nodes to create Workflowy meeting nodes. c) Run meeting-prep.py --meeting-id EVENT_ID to generate talking points. d) Run workflowy-sync.py --push-bullets EVENT_ID to push AI bullets to Workflowy. 4) Format the brief using the template in CRONS.md. Include talking points, open items with attendees, and a tomorrow preview. 5) Write to cache/morning-meeting-brief.txt. 6) Run: python3 ~/.openclaw/meetings-coach-workspace/scripts/timed-deliver.py cache/morning-meeting-brief.txt --token-env MEETINGS_BOT_TOKEN. 7) Update your status file."
echo "  [1/6] morning-meeting-brief (daily 11:55 UTC, deliver at 12:00 UTC / 5:00 AM PT)"

# 2. Pre-meeting alert — every 30 minutes (SILENT when no meetings approaching)
oc cron add \
  --agent meetings-coach \
  --name "pre-meeting-alert" \
  --cron "*/30 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Check for upcoming meetings needing alerts. Run gcal-fetch.py for events in the next 2 hours. Filter to real meetings starting in 15-45 minutes. Read sent-alerts.json to skip already-alerted meetings. For any un-alerted meeting: check cache for existing prep, generate if missing via meeting-prep.py. Send Telegram: '🐷🔍 Heads up — meeting in {N} min\n📋 {Title}\n👥 {Attendees}\n🎯 {Top bullets}\n📌 {Open items}'. Record in sent-alerts.json. If no meetings approaching: produce NO output."
echo "  [2/6] pre-meeting-alert (every 30 min, silent when no meetings)"

# 3. Post-meeting scan — every 30 minutes at :15 and :45 (SILENT when no transcripts)
oc cron add \
  --agent meetings-coach \
  --name "post-meeting-scan" \
  --cron "15,45 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Scan for recently-ended meetings and new transcripts. 1) Run gcal-fetch.py and identify meetings ended in the last 60 minutes. 2) Run transcript-scan.py to fetch Krisp transcripts and match to meetings. 3) Read the output. If items were extracted and staged: send a debrief summary on Telegram with action items, decisions, follow-ups. Include '/confirm to save to brain · /dismiss N to skip item N'. 4) If no transcripts found: produce NO output. 5) Update status file."
echo "  [3/6] post-meeting-scan (every 30 min at :15,:45, silent when no transcripts)"

# 4. Commitment follow-up — daily at 16:00 UTC (9:00 AM PT) (SILENT when all clear)
oc cron add \
  --agent meetings-coach \
  --name "commitment-follow-up" \
  --cron "0 16 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Check open commitments from meetings. Run: python3 ~/.openclaw/meetings-coach-workspace/scripts/commitment-tracker.py. Read the JSON output. If there are overdue or approaching items: send a follow-up summary on Telegram using the format in CRONS.md. If no actionable items: produce NO output. Update status file."
echo "  [4/6] commitment-follow-up (daily 16:00 UTC, silent when all clear)"

# 5. Heartbeat — every 30 minutes (SILENT on success)
oc cron add \
  --agent meetings-coach \
  --name "heartbeat" \
  --cron "*/30 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Update your heartbeat. Write the current UTC timestamp to last_heartbeat in ~/Dropbox/openclaw-backup/agents/meetings-coach.status.md. Verify these files exist: meeting-config.json, sent-alerts.json. Check cache/ for prep files older than 14 days — prune if found. Produce NO output if everything is normal."
echo "  [5/6] heartbeat (silent on success)"

# 6. Weekly review — Friday at 00:00 UTC (5:00 PM PT Thursday)
oc cron add \
  --agent meetings-coach \
  --name "weekly-review" \
  --cron "0 0 * * 5" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Generate the weekly meeting review. 1) Run gcal-fetch.py --days 7 to look back at this week's meetings. 2) Run commitment-tracker.py to get open commitments. 3) Compile: meetings held, prep generated, transcripts processed, commitments created/resolved/overdue. 4) List all unresolved commitments. 5) Format using the template in CRONS.md. 6) Deliver to Telegram."
echo "  [6/6] weekly-review (Friday 00:00 UTC)"

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
cat "$BRAIN/agents/meetings-coach.status.md"
echo ""

echo "Workspace:"
ls -la "$WORKSPACE/"
echo ""

echo "Scripts:"
ls -la "$WORKSPACE/scripts/"
echo ""

echo "Meeting config:"
cat "$WORKSPACE/meeting-config.json"
echo ""

echo "State files:"
cat "$WORKSPACE/sent-alerts.json"
echo ""
cat "$WORKSPACE/processed-transcripts.json"
echo ""

echo "Immutable files:"
lsattr "$WORKSPACE/SOUL.md" "$WORKSPACE/IDENTITY.md" 2>/dev/null || echo "  (lsattr not available)"
echo ""

echo "============================================"
echo "  Deployment complete!"
echo ""
echo "  Next steps:"
echo "  1. /start @openclaw_sergeant_murphy_bot on Telegram"
echo "  2. Approve pairing: oc pairing approve telegram <CODE>"
echo "  3. Run OAuth flow: python3 $WORKSPACE/scripts/gcal-auth.py"
echo "     (interactive — opens browser URL, paste auth code)"
echo "  4. Set WORKFLOWY_API_KEY in .env (from Workflowy settings)"
echo "  5. Set up Krisp MCP auth: python3 $WORKSPACE/scripts/transcript-scan.py --auth"
echo "  6. Smoke test: oc cron run <morning-meeting-brief-id>"
echo "  7. Test on-demand: send '/today' on Telegram"
echo ""
echo "  Run test suite:"
echo "  bash ~/openclaw-tests/test-agent.sh meetings-coach"
echo "============================================"
