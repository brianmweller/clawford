#!/usr/bin/env bash
# set-bot-descriptions.sh — set the empty-chat descriptions for all
# Busytown agent bots. Idempotent: safe to re-run.
#
# Telegram has TWO description fields per bot:
#
#   short_description (≤120 chars) — shown in chat list previews +
#                                    the empty-chat window header
#   description       (≤512 chars) — shown above the profile pic
#                                    + Start button when you open
#                                    a fresh chat with the bot
#
# Without these set, the empty-chat window is just blank — no hint
# of what the agent does. set-bot-commands.sh sets the slash-command
# menu but does NOT touch descriptions; this script handles the gap.
#
# Usage — from host:
#   bash ~/repo/ops/scripts/set-bot-descriptions.sh
#
# Environment: requires the per-agent bot token env vars
# (TELEGRAM_BOT_TOKEN, NEWSDIGEST_BOT_TOKEN, SHOPPING_BOT_TOKEN,
# FAMILYCAL_BOT_TOKEN, MEETINGS_BOT_TOKEN, CONNECTOR_BOT_TOKEN).
# Sourced from ~/clawford/.env if not already exported.

set -euo pipefail

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -f ~/clawford/.env ]; then
    set -a
    # shellcheck disable=SC1090
    source ~/clawford/.env
    set +a
fi


set_descriptions() {
    local name="$1"
    local token="$2"
    local short="$3"
    local long="$4"

    if [ -z "$token" ]; then
        echo "  ✗ $name — token env var empty; skipping"
        return
    fi

    # Use Python for the JSON body — git-bash curl on Windows mangles
    # UTF-8 quotes and the description text contains apostrophes,
    # em-dashes, and emoji on some agents. Python's urllib + json
    # handles encoding cleanly and is available everywhere.
    local result
    result=$(python3 - "$token" "$short" "$long" <<'PYEOF'
import json
import sys
import urllib.request

token, short, long_desc = sys.argv[1], sys.argv[2], sys.argv[3]


def call(method, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "description": str(e)}


short_result = call("setMyShortDescription", {"short_description": short})
long_result = call("setMyDescription", {"description": long_desc})

if short_result.get("ok") and long_result.get("ok"):
    print("OK")
else:
    parts = []
    if not short_result.get("ok"):
        parts.append(f"short: {short_result.get('description', 'unknown')}")
    if not long_result.get("ok"):
        parts.append(f"long: {long_result.get('description', 'unknown')}")
    print("FAIL " + " | ".join(parts))
PYEOF
    )

    if [ "$result" = "OK" ]; then
        echo "  ✓ $name"
    else
        echo "  ✗ $name — $result"
    fi
}


echo "Setting Telegram bot descriptions..."
echo ""

# ── Mr Fixit (fix-it) ───────────────────────────────────────
set_descriptions "Mr Fixit" "${TELEGRAM_BOT_TOKEN:-}" \
    "Your fleet operator — watches every Busytown agent and rings the bell when something breaks." \
    "Mr Fixit — the on-duty operator for the Busytown agent fleet. Every 15 minutes he probes shopping, family-calendar, meetings-coach, news-digest, connector, and his own services; alerts you when anything degrades; and assembles the morning status report at 5 AM PT. Part of the Busytown OpenClaw network."

# ── Hilda Hippo (shopping) ──────────────────────────────────
set_descriptions "Hilda Hippo" "${SHOPPING_BOT_TOKEN:-}" \
    "Your shopping concierge — grocery list, Amazon and Costco orders, morning delivery digest." \
    "Hilda Hippo — your shopping concierge. Tracks Amazon and Costco orders, manages Subscribe & Save, delivers the morning arrivals digest at 5 AM PT, and handles reorders on demand. Try /arriving for what's coming today, /grocery to manage the list, /reorder to repeat a past purchase. Part of the Busytown OpenClaw network."

# ── Mistress Mouse (family-calendar) ────────────────────────
set_descriptions "Mistress Mouse" "${FAMILYCAL_BOT_TOKEN:-}" \
    "Your family calendar keeper — schedules, reminders, school events." \
    "Mistress Mouse — keeper of the family calendar. Delivers the morning briefing at 5 AM PT, fires reminders 30 min before events, watches school activity emails for cancellations and changes, and parses incoming WhatsApp coordination. Try /today, /tomorrow, /week. Part of the Busytown OpenClaw network."

# ── Sergeant Murphy (meetings-coach) ────────────────────────
set_descriptions "Sergeant Murphy" "${MEETINGS_BOT_TOKEN:-}" \
    "Your meetings sergeant — preps you before, debriefs you after, tracks every open commitment." \
    "Sergeant Murphy — on duty for every meeting on your calendar. Preps you with attendees and history before each one, captures action items from Krisp recordings afterwards, and tracks open commitments until they close. Try /today, /prep 2pm, /commitments. Part of the Busytown OpenClaw network."

# ── Lowly Worm (news-digest) ────────────────────────────────
set_descriptions "Lowly Worm" "${NEWSDIGEST_BOT_TOKEN:-}" \
    "Your morning newsreader — curated digest from feeds and LinkedIn, ranked to your taste." \
    "Lowly Worm — your morning newsreader. Pulls RSS feeds and LinkedIn updates overnight, ranks them against your preference model, and delivers a curated 15-item digest at 5 AM PT. Tap the 👍/👎/📖 buttons on each item to teach him what you actually care about. Part of the Busytown OpenClaw network."

# ── Huckle Cat (connector) ──────────────────────────────────
# The original short/long was set manually before this script existed.
# Re-canonicalizing here with the same voice so the script is the
# single source of truth.
set_descriptions "Huckle Cat" "${CONNECTOR_BOT_TOKEN:-}" \
    "Your relationship memory — never forget a name, birthday, or promise." \
    "Huckle Cat — your relationship memory. Tracks who matters, surfaces overdue check-ins each morning at 5 AM PT, manages contact facts and notes, and proposes outreach. The context that makes a catch-up feel human. Part of the Busytown OpenClaw network."

echo ""
echo "Done. Re-run anytime — Telegram caches client-side, so close/reopen the chat to see changes."
