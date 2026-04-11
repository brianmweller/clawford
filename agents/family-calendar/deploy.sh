#!/usr/bin/env bash
# Deploy Mistress Mouse (Family Calendar) — Run this AFTER `openclaw agents add family-calendar`
# Usage: bash /tmp/deploy-family-calendar.sh
#
# Prerequisites:
#   - Docker container running: cd ~/openclaw && docker compose up -d
#   - openclaw agents add family-calendar (interactive onboarding completed inside container)
#   - Device pairing approved
#   - SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md, MEMORY.md in /tmp/
#   - scripts/ directory with all Python scripts in /tmp/
#   - .env with TELEGRAM_CHAT_ID and FAMILYCAL_BOT_TOKEN in /tmp/ or ~/openclaw/
#   - google-api-python-client, google-auth-httplib2, google-auth-oauthlib installed in Docker image

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
WORKSPACE="$HOME/.openclaw/family-calendar-workspace"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID in .env}"
FAMILYCAL_BOT_TOKEN="${FAMILYCAL_BOT_TOKEN:?Set FAMILYCAL_BOT_TOKEN in .env}"
TELEGRAM_ACCOUNT="familycal"
COMPOSE_FILE="$HOME/openclaw/docker-compose.yml"

# OpenClaw CLI wrapper — runs through Docker
oc() {
    docker compose -f "$COMPOSE_FILE" exec -T openclaw-gateway openclaw "$@"
}

echo "============================================"
echo "  Mistress Mouse (Family Calendar) — Deployment"
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
for script in gcal-fetch.py gcal-auth.py gcal-write.py reminder-check.py timed-deliver.py activity-email-check.py gmail-invite-check.py chat-parse-schedule.py; do
    if [ -f "/tmp/scripts/$script" ]; then
        cp "/tmp/scripts/$script" "$WORKSPACE/scripts/$script"
        echo "  Copied scripts/$script -> $WORKSPACE/scripts/$script"
    else
        echo "  WARNING: /tmp/scripts/$script not found — skipping"
    fi
done

# Copy calendar config
if [ -f "/tmp/calendar-config.json" ]; then
    cp "/tmp/calendar-config.json" "$WORKSPACE/calendar-config.json"
    echo "  Copied calendar-config.json -> $WORKSPACE/calendar-config.json"
fi

echo ""

# ── Step 2: Create Status File ───────────────────────────────

echo "Step 2: Initializing status file..."

cat > "$BRAIN/agents/family-calendar.status.md" << 'EOF'
# Family Calendar — Status

- **last_heartbeat:** —
- **status:** initializing
- **last_cron_run:** —
- **last_cron_result:** —
- **calendars_configured:** 2
- **google_auth:** unknown
- **error_log:** none
EOF

echo "  Written: $BRAIN/agents/family-calendar.status.md"
echo ""

# ── Step 3: Seed State Files ────────────────────────────────

echo "Step 3: Seeding state files..."

# Seed empty sent-reminders
cat > "$WORKSPACE/sent-reminders.json" << 'EOF'
{
  "reminders": {},
  "last_pruned": null
}
EOF

echo "  Written: $WORKSPACE/sent-reminders.json"

# Seed calendar config if not already copied
if [ ! -f "$WORKSPACE/calendar-config.json" ]; then
    cat > "$WORKSPACE/calendar-config.json" << 'EOF'
{
  "calendars": [
    {"id": "sam.smith@example.com", "label": "Sam", "emoji": "👨"},
    {"id": "alex.rivera@example.com", "label": "Alex", "emoji": "👩"}
  ],
  "timezone": "America/Los_Angeles"
}
EOF
    echo "  Written: $WORKSPACE/calendar-config.json"
fi

echo ""

# ── Step 4: Configure Telegram Channel + Binding ─────────────

echo "Step 4: Configuring Telegram channel + binding..."

oc channels add --channel telegram \
  --token "$FAMILYCAL_BOT_TOKEN" \
  --account "$TELEGRAM_ACCOUNT" \
  --name "Mistress Mouse" 2>/dev/null || true
echo "  Telegram account '$TELEGRAM_ACCOUNT' configured"

oc agents bind --agent family-calendar --bind "telegram:$TELEGRAM_ACCOUNT" 2>/dev/null || true
echo "  Agent family-calendar bound to telegram:$TELEGRAM_ACCOUNT"

echo ""
echo "  NOTE: You must /start the Mistress Mouse bot on Telegram and approve pairing:"
echo "  docker compose -f ~/openclaw/docker-compose.yml exec openclaw-gateway openclaw pairing approve telegram <CODE>"
echo ""

# ── Step 5: Set Up Exec Approvals ────────────────────────────

echo "Step 5: Setting exec approvals..."

oc approvals allowlist add --agent family-calendar "/usr/bin/*"
echo "  Added /usr/bin/* to allowlist"

oc approvals allowlist add --agent family-calendar "/bin/*"
echo "  Added /bin/* to allowlist"

oc approvals allowlist add --agent family-calendar "/usr/local/bin/*"
echo "  Added /usr/local/bin/* to allowlist"

# Per-agent exec policy: full (allowlist can't handle LLM compound commands).
oc config set tools.exec.ask off
echo "  Exec approvals set (policy=full per agent, ask=off)"

echo ""

# ── Step 6: Register Crons ───────────────────────────────────

echo "Step 6: Registering 7 crons..."

# 1. Morning briefing — daily at 11:50 UTC (4:50 AM PT), timed delivery at 12:00 UTC (5:00 AM PT)
oc cron add \
  --agent family-calendar \
  --name "morning-briefing" \
  --cron "50 11 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --message "Generate and deliver the morning family briefing. 1) Run: python3 ~/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --days 2. Read the JSON output — these are today's and tomorrow's events across all family calendars. 2) Format the briefing using the template in CRONS.md. Group events by time blocks (Morning, Afternoon, Evening). Use family member emoji (👨 Sam, 👩 Alex, 🧒 Avery, 👶 Jordan, 🏠 Jamie). Flag any conflicts with ⚠️. Include a tomorrow preview. Check if today is a standard routine day (see MEMORY.md) — if no extra events, say 'Standard {weekday} — no exceptions.' Note the pickup arrangement for today (Mon=Sam+swimming, Fri=Sam, other=Jamie). 3) Write the formatted output to cache/morning-briefing.txt. 4) Run: python3 ~/.openclaw/family-calendar-workspace/scripts/timed-deliver.py cache/morning-briefing.txt --token-env FAMILYCAL_BOT_TOKEN. 5) Update your status file."
echo "  [1/7] morning-briefing (daily 11:50 UTC, deliver at 12:00 UTC / 5:00 AM PT)"

# 2. Reminder check — every 5 minutes (SILENT when no reminders)
oc cron add \
  --agent family-calendar \
  --name "reminder-check" \
  --cron "*/5 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Check for upcoming events that need reminders. Run: python3 ~/.openclaw/family-calendar-workspace/scripts/reminder-check.py. Read the JSON output. If the output contains reminders: for each one, send a Telegram message '🐭 Heads up — {emoji} {person} {event} in {minutes} min ({location})'. If the output is empty (no reminders needed): produce NO output — do not send any message. Update your status file only if reminders were sent."
echo "  [2/7] reminder-check (every 5 min, silent when no reminders)"

# 3. Heartbeat — every 30 minutes (SILENT on success)
oc cron add \
  --agent family-calendar \
  --name "heartbeat" \
  --cron "*/30 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Update your heartbeat. Write the current UTC timestamp to last_heartbeat in ~/Dropbox/openclaw-backup/agents/family-calendar.status.md. Verify these files exist: calendar-config.json, sent-reminders.json. Check if sent-reminders.json has entries older than 48 hours — if so, prune them. Produce NO output if everything is normal."
echo "  [3/7] heartbeat (silent on success)"

# 4. Activity email check — every 2 hours at :15 (SILENT when no items)
oc cron add \
  --agent family-calendar \
  --name "activity-email-check" \
  --cron "15 */2 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Check Gmail for emails from activity providers. Run: python3 ~/.openclaw/family-calendar-workspace/scripts/activity-email-check.py. Read the JSON output — these are emails from Example Preschool, Example Swim School, and Example Ballet Studio in the last 48 hours. For each email with schedule-relevant content (closures, cancellations, action items, event changes): classify the urgency and send a Telegram message. Closures/cancellations: '🐭 ⚠️ {source}: {summary}'. Action items: '🐭 📋 {source}: {summary}'. Events/FYI: '🐭 📌 {source}: {summary}'. If no actionable items found: produce NO output. Update your status file."
echo "  [4/7] activity-email-check (every 2h at :15, silent when no items)"

# 5. Gmail invite check — every 3 hours at :30 (SILENT when no invites)
oc cron add \
  --agent family-calendar \
  --name "gmail-invite-check" \
  --cron "30 */3 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Check Gmail for calendar invites. Run: python3 ~/.openclaw/family-calendar-workspace/scripts/gmail-invite-check.py. Read the JSON output — these are new calendar invites (ICS attachments with METHOD:REQUEST). For each invite: send a Telegram message '🐭 New invite: {subject} on {date} from {organizer}. Accept?'. If no new invites: produce NO output."
echo "  [5/7] gmail-invite-check (every 3h at :30, silent when no invites)"

# 6. Weekly overview — Sunday at 01:00 UTC (6 PM PT Saturday)
oc cron add \
  --agent family-calendar \
  --name "weekly-overview" \
  --cron "0 1 * * 0" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Generate and deliver the weekly schedule overview. Run: python3 ~/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --days 7. Format as a day-by-day overview for the upcoming week. For each day: list key events with times, attendees, and locations. Flag any conflicts with ⚠️. Note pickup arrangements per day (Mon=Sam+swimming, Fri=Sam, other=Jamie). Include a summary line: '🐭 {N} events this week · {conflicts} conflicts'. Deliver to Telegram."
echo "  [6/7] weekly-overview (Sunday 01:00 UTC / 6 PM PT Saturday)"

# 7. WhatsApp chat scan — every 2 hours at :45 (SILENT when no items)
oc cron add \
  --agent family-calendar \
  --name "whatsapp-chat-scan" \
  --cron "45 */2 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Scan WhatsApp messages for schedule-relevant content. Run: python3 ~/.openclaw/family-calendar-workspace/scripts/chat-parse-schedule.py. Read the JSON output — these are recent WhatsApp messages from the familycal-wa channel. For each message with schedule-relevant content (cancellations, time changes, pickup changes, new events): send a Telegram alert to Sam: '🐭 📱 WhatsApp: {person} — {summary}. Suggested action: {action}.' If no schedule-relevant messages: produce NO output. NEVER post to WhatsApp groups automatically — all output goes to Sam on Telegram."
echo "  [7/7] whatsapp-chat-scan (every 2h at :45, silent when no items)"

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
cat "$BRAIN/agents/family-calendar.status.md"
echo ""

echo "Workspace:"
ls -la "$WORKSPACE/"
echo ""

echo "Scripts:"
ls -la "$WORKSPACE/scripts/"
echo ""

echo "Calendar config:"
cat "$WORKSPACE/calendar-config.json"
echo ""

echo "Sent reminders:"
cat "$WORKSPACE/sent-reminders.json"
echo ""

echo "Immutable files:"
lsattr "$WORKSPACE/SOUL.md" "$WORKSPACE/IDENTITY.md" 2>/dev/null || echo "  (lsattr not available)"
echo ""

echo "============================================"
echo "  Deployment complete!"
echo ""
echo "  Next steps:"
echo "  1. /start @openclaw_mistress_mouse_bot on Telegram"
echo "  2. Approve pairing: oc pairing approve telegram <CODE>"
echo "  3. Run OAuth flow: python3 $WORKSPACE/scripts/gcal-auth.py"
echo "     (interactive — opens browser URL, paste auth code)"
echo "  4. Smoke test: oc cron run <morning-briefing-id>"
echo "  5. Test on-demand: send '/today' on Telegram"
echo ""
echo "  Run test suite:"
echo "  bash ~/openclaw-tests/test-agent.sh family-calendar"
echo "============================================"
