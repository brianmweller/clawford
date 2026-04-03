# Chapter 5: Telegram Bots

Each agent gets its own bot. Otherwise you can't tell who's talking to you.

---

## Why separate bots

If all agents share one bot, every message comes from the same sender. At 3 AM when an agent alerts you, you want to know instantly whether it's Fix-It reporting a down agent or the Shopping agent confirming a purchase. Separate bots give each agent:

- Its own name and avatar in Telegram
- A dedicated chat thread
- Clear identity — no confusion about which agent sent what

## Creating a bot

For each agent:

1. Open Telegram, message **@BotFather**
2. Send `/newbot`
3. **Display name:** The agent's name (e.g., "Mr Fixit", "News Digest")
4. **Username:** `openclaw_{agent}_bot` (must end in `bot`)
5. Copy the bot token

Optional but recommended: use `/setuserpic` in BotFather to give the bot an avatar that matches the agent's emoji from IDENTITY.md.

## Adding the bot to OpenClaw

Run these inside Docker (via the `oc` wrapper):

```bash
# Add the bot as a channel account
oc channels add --channel telegram \
  --token "{bot-token}" \
  --account "{agent-id}" \
  --name "{Display Name}"

# Bind the agent to this account
oc agents bind --agent {agent-id} --bind telegram:{agent-id}
```

Example for Mr Fixit:

```bash
oc channels add --channel telegram \
  --token "$FIXIT_BOT_TOKEN" \
  --account fixit \
  --name "Mr Fixit"

oc agents bind --agent fix-it --bind telegram:fixit
```

## Pairing

After adding the bot, you need to pair it:

1. Send `/start` to the new bot on Telegram
2. The bot may display a pairing code
3. Approve it:

```bash
oc pairing approve telegram {CODE}
```

If the bot responds to `/start` with a conversation (no pairing code), it's already paired.

## Cron delivery

Every cron job must include the bot's account ID for Telegram delivery:

```bash
oc cron add \
  --agent {agent-id} \
  --name "{cron-name}" \
  --cron "{schedule}" \
  --message "{instructions}" \
  --to {chat-id} \
  --account {agent-id} \
  --announce
```

The three flags that matter:
- `--to {chat-id}` — your Telegram user ID (numeric, same across all bots)
- `--account {agent-id}` — routes through this agent's bot
- `--announce` — enables delivery to the chat

> **WARNING:** Omitting any of these three flags causes silent delivery failure. The cron runs, the agent works, but no Telegram message arrives.

## Secrets management

Store bot tokens in `.env`, never in code:

```bash
# .env
FIXIT_BOT_TOKEN=1234567890:ABCDEFghijklmnop
NEWSDIGEST_BOT_TOKEN=0987654321:QRSTUVwxyz
```

Deploy scripts read from `.env` at runtime. The `.env` file is gitignored.

## Finding your chat ID

Your Telegram chat ID is the same across all bots — it's your user ID, not a per-bot value. To find it:

1. Send any message to one of your bots
2. Run:

```bash
curl -s "https://api.telegram.org/bot{BOT_TOKEN}/getUpdates" | python3 -m json.tool | grep -A2 '"chat"'
```

The `"id"` field inside `"chat"` is your chat ID.

---

Next: [Chapter 6 — Testing](06-testing.md)
