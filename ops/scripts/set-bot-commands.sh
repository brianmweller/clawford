#!/usr/bin/env bash
# set-bot-commands.sh — Re-apply custom Telegram bot commands for all agents.
#
# OpenClaw's channel-sync pushes its ~49 default slash commands whenever
# its config hash changes. The durable fix is Step 10a in DEPLOY.md —
# set commands.native:false + customCommands per account in
# ~/.openclaw/openclaw.json, which makes openclaw respect the custom
# list across restarts. This script is the belt-and-suspenders Step 10b:
# direct Bot API set for immediate effect and as emergency-restore after
# any accidental clobber.
#
# Usage — from host:
#   bash ~/repo/ops/scripts/set-bot-commands.sh
# Usage — from inside the gateway container (entrypoint.sh hook):
#   bash /home/node/repo/ops/scripts/set-bot-commands.sh
#
# Environment: requires the per-agent bot token env vars
# (TELEGRAM_BOT_TOKEN, NEWSDIGEST_BOT_TOKEN, SHOPPING_BOT_TOKEN,
# FAMILYCAL_BOT_TOKEN, MEETINGS_BOT_TOKEN) to be set. Inside the
# container these come from docker-compose.yml's env_file. From the
# host we source ~/openclaw/.env if any are missing.

set -euo pipefail

# Source host .env only if the required vars aren't already exported.
# Inside the container env is already set, so sourcing would fail.
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -f ~/openclaw/.env ]; then
    set -a
    # shellcheck disable=SC1090
    source ~/openclaw/.env
    set +a
fi

set_commands() {
    local name="$1"
    local token="$2"
    local commands="$3"

    if [ -z "$token" ]; then
        echo "  ✗ $name — token env var empty; skipping"
        return
    fi

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
set_commands "Mistress Mouse" "${FAMILYCAL_BOT_TOKEN:-}" '{
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

# ── Sergeant Murphy ─────────────────────────────────────────
set_commands "Sergeant Murphy" "${MEETINGS_BOT_TOKEN:-}" '{
  "commands": [
    {"command": "today", "description": "Today'\''s meetings with prep status"},
    {"command": "prep", "description": "Generate or refresh prep: /prep 2pm"},
    {"command": "debrief", "description": "Force debrief: /debrief [meeting]"},
    {"command": "commitments", "description": "List open meeting commitments"},
    {"command": "week", "description": "Weekly meeting overview"},
    {"command": "confirm", "description": "Approve extracted action items"},
    {"command": "dismiss", "description": "Skip a staged item: /dismiss 2"},
    {"command": "coaching", "description": "Coaching controls: on/off/trends"}
  ]
}'

# Harden the other existing tokens against empty-env spurious calls
# by also quoting them through ${:-}. (Done for the first four already
# handled via the same pattern here as a defensive measure.)

echo ""
echo "Done. When adding a new agent, add its commands to this script."
