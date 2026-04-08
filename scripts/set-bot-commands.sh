#!/usr/bin/env bash
# set-bot-commands.sh — Re-apply custom Telegram bot commands for all agents.
#
# OpenClaw overwrites bot commands with its own 48 slash commands on every
# gateway restart. Run this after any `docker compose restart` or `up -d`.
#
# Usage: bash scripts/set-bot-commands.sh
# Or from VPS: bash /tmp/set-bot-commands.sh

set -euo pipefail

# Load secrets
if [ -f ~/openclaw/.env ]; then
    source ~/openclaw/.env
elif [ -f .env ]; then
    source .env
fi

set_commands() {
    local name="$1"
    local token="$2"
    local commands="$3"

    local result
    result=$(curl -sf "https://api.telegram.org/bot${token}/setMyCommands" \
        -H "Content-Type: application/json" \
        -d "$commands" 2>&1) || true

    if echo "$result" | grep -q '"ok":true'; then
        echo "  ✓ $name"
    else
        echo "  ✗ $name — $result"
    fi
}

echo "Setting Telegram bot commands..."
echo ""

# ── Mr Fixit ────────────────────────────────────────────────
set_commands "Mr Fixit" "$TELEGRAM_BOT_TOKEN" '{
  "commands": [
    {"command": "status", "description": "System status report"},
    {"command": "health", "description": "Quick health check all agents"},
    {"command": "agents", "description": "List all agents and their status"}
  ]
}'

# ── Lowly Worm ──────────────────────────────────────────────
set_commands "Lowly Worm" "$NEWSDIGEST_BOT_TOKEN" '{
  "commands": [
    {"command": "digest", "description": "Get today'\''s news digest now"},
    {"command": "topics", "description": "Show tracked topics"},
    {"command": "search", "description": "Search for a topic"}
  ]
}'

# ── Hilda Hippo ─────────────────────────────────────────────
set_commands "Hilda Hippo" "$SHOPPING_BOT_TOKEN" '{
  "commands": [
    {"command": "arriving", "description": "What'\''s arriving? Amazon + Costco"},
    {"command": "track", "description": "Track a specific item"},
    {"command": "subscriptions", "description": "List Subscribe & Save items"},
    {"command": "grocery", "description": "View/manage grocery list"},
    {"command": "reorder", "description": "Reorder a past purchase"},
    {"command": "find", "description": "Search for a new item"},
    {"command": "confirm", "description": "Approve a pending cart action"},
    {"command": "cancel", "description": "Cancel a pending action"}
  ]
}'

# ── Mistress Mouse ──────────────────────────────────────────
set_commands "Mistress Mouse" "$FAMILYCAL_BOT_TOKEN" '{
  "commands": [
    {"command": "today", "description": "Today'\''s full family schedule"},
    {"command": "tomorrow", "description": "Tomorrow'\''s schedule"},
    {"command": "week", "description": "This week'\''s overview"},
    {"command": "add", "description": "Add event: /add Avery dentist Thu 2pm"},
    {"command": "move", "description": "Reschedule: /move dentist to Fri 3pm"},
    {"command": "cancel", "description": "Remove event: /cancel swimming Mon"},
    {"command": "confirm", "description": "Approve a pending calendar change"},
    {"command": "nevermind", "description": "Cancel a pending change"}
  ]
}'

# ── Sergeant Murphy ──────────────────────────────────────────
set_commands "Sergeant Murphy" "$MEETINGS_BOT_TOKEN" '{
  "commands": [
    {"command": "today", "description": "Today'\''s meetings with prep status"},
    {"command": "prep", "description": "Generate prep for a meeting"},
    {"command": "debrief", "description": "Process debrief for a meeting"},
    {"command": "commitments", "description": "List open commitments"},
    {"command": "week", "description": "This week'\''s meeting overview"},
    {"command": "confirm", "description": "Approve extracted action items"},
    {"command": "dismiss", "description": "Dismiss an extracted item"}
  ]
}'

echo ""
echo "Done. When adding a new agent, add its commands to this script."
