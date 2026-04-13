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

## Commands and descriptions

Out of the box, a fresh Telegram bot looks generic: empty chat window, no slash-command menu, no hint of what the agent does. Two Telegram Bot API surfaces fix this — and both need to be set explicitly because the @BotFather flow doesn't.

**Slash commands** (`setMyCommands`) populate the `/` picker that appears when the user starts typing a slash. Each command shows in the picker with a short label.

**Descriptions** (`setMyDescription` and `setMyShortDescription`) populate the *empty-chat window* — the screen the user sees the first time they open a chat with the bot, or after they clear the chat history. Without these, the empty chat is just a blank window with no clue what the bot is for.

The two fields are separate:

| Field | Limit | Where it appears |
|---|---|---|
| `short_description` | 120 chars | Chat list previews, empty-chat window header |
| `description` | 512 chars | Above the **Start** button on a fresh chat, before the first message |

Both fields must be set via the Bot API — there is no @BotFather command for them. We have two canonical scripts that handle the entire fleet at once:

```bash
# From the host (or inside the gateway container):
bash ~/repo/ops/scripts/set-bot-commands.sh
bash ~/repo/ops/scripts/set-bot-descriptions.sh
```

Both scripts are idempotent (safe to re-run), use Python's `urllib.request` for UTF-8-safe JSON bodies (git-bash `curl` on Windows mangles quotes), and read tokens from `~/openclaw/.env`. They also run automatically on every container start via `entrypoint.sh` hooks at +25s and +30s post-start, so a `docker compose restart` re-applies both — no manual step needed after a normal redeploy.

> **OpenClaw caveat for commands only.** OpenClaw's channel-sync re-applies its built-in 49 default commands (`/help`, `/status`, `/context`, `/exec`, …) whenever its config hash changes — they'll clobber whatever you set via `setMyCommands` unless you set `commands.native: false` + `customCommands` in `~/.openclaw/openclaw.json`. The entrypoint hook is the belt-and-suspenders backup. Description fields are NOT touched by openclaw, so `set-bot-descriptions.sh` has no clobber risk.

After running either script, **close and reopen the chat in your Telegram client** to see the change. Telegram aggressively client-caches both command menus and descriptions; pull-down to refresh isn't always enough — sometimes you need to close the chat entirely.

When you add a new agent, edit both scripts to add the agent's bot. The descriptions follow Huckle Cat's tone — second person, action-oriented, ending with "Part of the Busytown OpenClaw network".

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
