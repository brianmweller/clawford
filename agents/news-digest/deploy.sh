#!/usr/bin/env bash
# Deploy Lowly Worm (News Digest) — Run this AFTER `openclaw agents add news-digest`
# Usage: bash /tmp/deploy-news-digest.sh
#
# Prerequisites:
#   - Docker container running: cd ~/openclaw && docker compose up -d
#   - openclaw agents add news-digest (interactive onboarding completed inside container)
#   - Device pairing approved
#   - SOUL.md, IDENTITY.md, TOOLS.md in /tmp/
#   - scripts/ directory with fetch-and-rank.py, update-preferences.py, on-demand.py in /tmp/
#   - .env with TELEGRAM_CHAT_ID, NEWSDIGEST_BOT_TOKEN, LINKEDIN_USER, LINKEDIN_PASS in /tmp/ or ~/openclaw/
#   - feedparser, linkedin-api, and openai pip packages installed in Docker image

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
WORKSPACE="$HOME/.openclaw/news-digest-workspace"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID in .env}"
NEWSDIGEST_BOT_TOKEN="${NEWSDIGEST_BOT_TOKEN:?Set NEWSDIGEST_BOT_TOKEN in .env}"
TELEGRAM_ACCOUNT="newsdigest"
COMPOSE_FILE="$HOME/openclaw/docker-compose.yml"

# OpenClaw CLI wrapper — runs through Docker
oc() {
    docker compose -f "$COMPOSE_FILE" exec -T openclaw-gateway openclaw "$@"
}

echo "============================================"
echo "  Lowly Worm (News Digest) — Deployment"
echo "  OpenClaw 2026.4.1 (Docker)"
echo "============================================"
echo ""

# ── Step 1: Install Configuration Files ──────────────────────

echo "Step 1: Installing config files..."

mkdir -p "$WORKSPACE"
mkdir -p "$WORKSPACE/scripts"
mkdir -p "$WORKSPACE/preferences"
mkdir -p "$WORKSPACE/cache"
mkdir -p "$WORKSPACE/logs"

for file in SOUL.md IDENTITY.md TOOLS.md; do
    if [ -f "/tmp/$file" ]; then
        cp "/tmp/$file" "$WORKSPACE/$file"
        echo "  Copied $file -> $WORKSPACE/$file"
    else
        echo "  WARNING: /tmp/$file not found — skipping"
    fi
done

# Copy Python scripts
for script in fetch-and-rank.py update-preferences.py on-demand.py; do
    if [ -f "/tmp/scripts/$script" ]; then
        cp "/tmp/scripts/$script" "$WORKSPACE/scripts/$script"
        echo "  Copied scripts/$script -> $WORKSPACE/scripts/$script"
    else
        echo "  WARNING: /tmp/scripts/$script not found — skipping"
    fi
done

echo ""

# ── Step 2: Create Status File ───────────────────────────────

echo "Step 2: Initializing status file..."

cat > "$BRAIN/agents/news-digest.status.md" << 'EOF'
# News Digest — Status

- **last_heartbeat:** —
- **status:** initializing
- **last_cron_run:** —
- **last_cron_result:** —
- **feeds_fetched:** 0
- **items_delivered:** 0
- **error_log:** none
- **token_usage_today:** 0
EOF

echo "  Written: $BRAIN/agents/news-digest.status.md"
echo ""

# ── Step 3: Seed Preference Model ────────────────────────────

echo "Step 3: Seeding preference model..."

cat > "$WORKSPACE/preferences/model.json" << 'EOF'
{
  "version": 1,
  "updated_at": null,
  "topic_weights": {
    "ai": 1.5,
    "economics": 1.5,
    "international_politics": 1.5,
    "us_domestic_policy": 1.3,
    "tech": 1.2,
    "regulation": 1.2,
    "markets": 1.0,
    "science": 1.0,
    "business": 1.0,
    "startups": 1.0
  },
  "source_weights": {
    "wsj": 1.0,
    "nyt": 1.0,
    "wapo": 1.0,
    "slate": 1.0,
    "google_news": 1.0,
    "linkedin": 1.0
  },
  "topic_vocabulary": {
    "ai": ["artificial intelligence", "machine learning", "neural network", "llm", "large language model", "openai", "anthropic", "deepmind", "gpt", "transformer", "generative ai", "chatbot", "ai safety", "ai regulation", "foundation model", "diffusion model"],
    "economics": ["economy", "gdp", "inflation", "federal reserve", "fed", "interest rate", "treasury", "fiscal", "monetary policy", "unemployment", "recession", "trade deficit", "tariff"],
    "international_politics": ["geopolitics", "nato", "china", "ukraine", "russia", "middle east", "diplomacy", "sanctions", "united nations", "eu", "european union", "brics", "south china sea", "taiwan"],
    "us_domestic_policy": ["congress", "senate", "legislation", "executive order", "supreme court", "regulation", "bipartisan", "democrat", "republican", "white house", "amendment", "filibuster", "cabinet"],
    "tech": ["software", "hardware", "startup", "silicon valley", "cybersecurity", "cloud computing", "quantum computing", "semiconductor", "chip", "apple", "google", "microsoft", "meta", "amazon"],
    "regulation": ["antitrust", "ftc", "sec", "compliance", "data privacy", "gdpr", "section 230", "content moderation"],
    "markets": ["stock", "s&p", "nasdaq", "dow", "bond", "yield", "crypto", "bitcoin", "ipo", "earnings", "wall street"],
    "science": ["research", "study", "breakthrough", "climate", "energy", "space", "nasa", "biotech", "pharmaceutical"],
    "business": ["merger", "acquisition", "revenue", "profit", "layoff", "ceo", "board", "valuation", "funding round"],
    "startups": ["venture capital", "seed round", "series a", "y combinator", "unicorn", "founder", "accelerator"]
  }
}
EOF

echo "  Written: $WORKSPACE/preferences/model.json"

# Create empty engagement log
touch "$WORKSPACE/preferences/engagement.jsonl"
echo "  Created: $WORKSPACE/preferences/engagement.jsonl"

echo ""

# ── Step 4: Configure Telegram Channel + Binding ─────────────

echo "Step 4: Configuring Telegram channel + binding..."

oc channels add --channel telegram \
  --token "$NEWSDIGEST_BOT_TOKEN" \
  --account "$TELEGRAM_ACCOUNT" \
  --name "Lowly Worm" 2>/dev/null || true
echo "  Telegram account '$TELEGRAM_ACCOUNT' configured"

oc agents bind --agent news-digest --bind "telegram:$TELEGRAM_ACCOUNT" 2>/dev/null || true
echo "  Agent news-digest bound to telegram:$TELEGRAM_ACCOUNT"

echo ""
echo "  NOTE: You must /start the Lowly Worm bot on Telegram and approve pairing:"
echo "  docker compose -f ~/openclaw/docker-compose.yml exec openclaw-gateway openclaw pairing approve telegram <CODE>"
echo ""

# ── Step 5: Set Up Exec Approvals ────────────────────────────

echo "Step 5: Setting exec approvals..."

oc approvals allowlist add --agent news-digest "/usr/bin/*"
echo "  Added /usr/bin/* to allowlist"

oc approvals allowlist add --agent news-digest "/bin/*"
echo "  Added /bin/* to allowlist"

oc approvals allowlist add --agent news-digest "/usr/local/bin/*"
echo "  Added /usr/local/bin/* to allowlist"

oc approvals allowlist add --agent news-digest "python3 ~/.openclaw/news-digest-workspace/scripts/*"
echo "  Added python3 scripts/* to allowlist"

oc approvals allowlist add --agent news-digest "python3 -"
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

echo "Step 6: Registering 3 crons..."

# 1. Morning edition — daily at 12:00 UTC (8 AM ET)
oc cron add \
  --agent news-digest \
  --name "morning-edition" \
  --cron "0 12 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --announce \
  --message "Generate and deliver the morning news digest. Run: python3 ~/.openclaw/news-digest-workspace/scripts/fetch-and-rank.py. This fetches all RSS feeds (NYT, WSJ, WaPo, Slate, Google News) and LinkedIn updates, deduplicates articles, extracts topics, and ranks them using the preference model at preferences/model.json. Read the ranked output from cache/ranked-$(date +%Y-%m-%d).json. Select the top 15-20 items. For each item, generate an extended headline: the original headline plus one sentence of context explaining why it matters or what's new. Group items by topic (🤖 AI & Tech, 💰 Economics, 🌍 World, 🏛️ US Policy, 🔗 LinkedIn, 📋 Also Noted). Format using the template in CRONS.md. Include item numbers so the reader can use /like, /dislike, /more commands. End with: 🐛 {count} items · {source_count} sources · Reply: /like 1, /more 3, /ask [topic]. If any feed failed, note it at the bottom. Update your status file with results."
echo "  [1/3] morning-edition (daily 12:00 UTC)"

# 2. Preference update — daily at 23:00 UTC (SILENT on success)
oc cron add \
  --agent news-digest \
  --name "preference-update" \
  --cron "0 23 * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Process today's engagement signals and update the preference model. Run: python3 ~/.openclaw/news-digest-workspace/scripts/update-preferences.py. This reads new entries from preferences/engagement.jsonl, applies multiplicative weight updates (thumbs_up: topic ×1.1 source ×1.05, thumbs_down: topic ×0.85 source ×0.95, expand: topic ×1.05), clamps weights to safe ranges, and writes updated model to preferences/model.json. If there are no new engagement events, skip the update silently. Update your status file with the number of events processed and produce NO output."
echo "  [2/3] preference-update (silent on success)"

# 3. Heartbeat — every 30 minutes (SILENT on success)
oc cron add \
  --agent news-digest \
  --name "heartbeat" \
  --cron "*/30 * * * *" \
  --to "$TELEGRAM_CHAT_ID" \
  --account "$TELEGRAM_ACCOUNT" \
  --no-deliver \
  --failure-alert --failure-alert-to "$TELEGRAM_CHAT_ID" --failure-alert-account-id "$TELEGRAM_ACCOUNT" --failure-alert-channel telegram \
  --message "Update your heartbeat. Write the current UTC timestamp to last_heartbeat in ~/Dropbox/openclaw-backup/agents/news-digest.status.md. Produce NO output."
echo "  [3/3] heartbeat (silent on success)"

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
cat "$BRAIN/agents/news-digest.status.md"
echo ""

echo "Workspace:"
ls -la "$WORKSPACE/"
echo ""

echo "Scripts:"
ls -la "$WORKSPACE/scripts/"
echo ""

echo "Preferences:"
ls -la "$WORKSPACE/preferences/"
echo ""

echo "Immutable files:"
lsattr "$WORKSPACE/SOUL.md" "$WORKSPACE/IDENTITY.md" 2>/dev/null || echo "  (lsattr not available)"
echo ""

echo "============================================"
echo "  Deployment complete!"
echo ""
echo "  Smoke test (use IDs from cron list above):"
echo "  oc() { docker compose -f ~/openclaw/docker-compose.yml exec -T openclaw-gateway openclaw \"\$@\"; }"
echo "  oc cron run <morning-edition-id>"
echo "  Send Telegram: '/ask artificial intelligence'"
echo ""
echo "  Run test suite:"
echo "  bash ~/openclaw-tests/test-agent.sh news-digest"
echo "============================================"
