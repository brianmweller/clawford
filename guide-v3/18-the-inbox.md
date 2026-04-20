# The inbox: making agents conversational

*Last updated: 2026-04-20 · Reading time: ~23 min · Difficulty: moderate*

> **TL;DR.** Every agent chapter up to this point describes **outbound** behavior — crons that fire, scripts that compose, Telegram messages that push to the operator. This chapter is the **inbound** side. A single inbox daemon long-polls every Telegram bot concurrently, routes each incoming message to the right agent, hands it to an LLM with a tool manifest the agent defines, lets the LLM call read tools (no side effects) or producer tools (which stage a pending action with inline buttons), and waits for the operator to tap **Confirm** or **Cancel** before anything mutates state. The result is that every agent in the fleet is conversational — the operator can message Mistress Mouse and say "add school pickup Thursday 3pm" and get a `[Create event]` `[Cancel]` button pair without a single line of agent-specific dispatcher code. A second, smaller listener covers the other inbound channel — real-time Gmail triage via Pub/Sub pull — so Huckle Cat drafts a reply in under a minute instead of waiting on the half-hour polling cron. The architecture is ~1200 lines of shared Python for the Telegram side, ~400 more for the Gmail side, a `tools.py` file per agent, and two systemd units on the VPS. This chapter covers all five Telegram components, the tools.py pattern, the inline-button UX, the Gmail push path, the deployment story, and the pitfalls.

## What changed

Before this chapter's architecture existed, Clawford agents were outbound-only. Crons fired on a schedule, scripts composed output, Telegram messages pushed to the operator's chat. The operator could read but not reply. Asking "what's arriving today?" meant SSHing to the VPS and running a script by hand.

The OpenClaw gateway used to fill this gap — it received Telegram updates, dispatched them to an LLM session, and routed the response back. The Clawford liberation deleted that gateway (see [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md)). The inbox daemon is its architectural successor — except it is ~1200 lines of plain Python instead of a container runtime, it uses native function-calling instead of prompt-based tool dispatch, and it stages every mutation behind a human-confirmation gate instead of executing directly.

## The five components

### 1. The inbox daemon (`telegram_inbox.py`)

A single async Python process that long-polls all six Telegram bots concurrently. One `asyncio` task per bot token, each hitting `getUpdates` with a 25-second long-poll timeout. A shared `httpx.AsyncClient` across all tasks to keep connection overhead minimal.

**Per-agent offset persistence.** Each bot's last-processed update ID is written to `~/.clawford/inbox/{agent-id}.offset` after every successful dispatch. On restart, the daemon resumes from the persisted offset, so no messages get re-processed and no messages get skipped. If the offset file is missing, the daemon starts from `offset=-1` (Telegram's "give me everything since the beginning of time" default), which is safe because the dispatcher is idempotent for read-tool calls and the pending-action flow is double-tap safe.

**Per-update error isolation.** If one message's dispatch throws an exception, the daemon logs the traceback, advances the offset anyway (so the bad message doesn't re-deliver forever), and continues processing the next update. A crash in one agent's tool executor does not take down the other five bots.

**Backoff on `getUpdates` failure.** If the Telegram API itself is unreachable, the task sleeps 5 seconds and retries. No exponential backoff — 5 seconds is enough for transient network blips, and a sustained Telegram outage is not something the daemon can fix by backing off harder.

**Kill-switch file.** If `~/.clawford/inbox-disabled` exists, the daemon exits cleanly at startup. This is the dev-vs-VPS toggle: when the operator wants to run the inbox locally against production bot tokens (for debugging or testing a new tool), creating this file on the VPS gracefully parks the production daemon so Telegram's single-poller-per-bot lock doesn't collide. Remove the file and restart the service to re-enable.

### 2. The dispatcher (`dispatcher.py`)

The stateless request handler. Called once per inbound Telegram update, wrapped in `asyncio.to_thread` by the daemon (so the sync tool-use loop doesn't block the async poll tasks).

The dispatcher's flow, in order:

1. **Chat ID gate.** Drop any update not from the operator's configured `TELEGRAM_CHAT_ID`. This is the single-user security boundary — there is no multi-user support, no role-based access, no session partitioning. One operator, one chat ID, and everything else is silently ignored. See [Ch 19 — Security and hardening](19-security-and-hardening.md) for the threat model.

2. **Callback shortcut fast-path.** If the update is a `callback_query` (the operator tapped an inline button), the dispatcher parses the callback data and handles it without invoking the LLM at all:
   - `confirm:{action_id}` — load the pending action, call the agent's `confirm_{kind}` executor with the stored payload, remove the action on success, reply with the result.
   - `cancel:{action_id}` — load the pending action, discard it, reply "Cancelled: {summary}."
   - `confirm_all:{batch_id}` — confirm every action in the batch, aggregate the results.
   - `cancel_all:{batch_id}` — cancel every action in the batch.
   - `like:{id}` / `dislike:{id}` / `more:{id}` — route directly to the agent's `record_engagement` executor (used by [Lowly Worm's](10-lowly-worm-newsfeed.md) article feedback buttons).
   
   The key design decision: **callback shortcuts bypass the LLM.** Tapping "Confirm" on a reorder button should not require a 2-second model round-trip to figure out what to do. The button's callback data contains everything the dispatcher needs.

3. **Agent config loading.** Import the agent's `tools.py` module dynamically. Read the agent's five identity docs — `SOUL.md` (values/principles, filesystem-immutable), `IDENTITY.md` (persona/voice, filesystem-immutable), `USER.md` (who the operator is), `AGENTS.md` (fleet map for cross-agent routing, filesystem-immutable), and `MEMORY.md` (learned rules, appendable via the `remember` tool) — from the **Dropbox brain** at `~/Dropbox/openclaw-backup/agents/<agent-id>/` to assemble the system prompt. Brain-sourced content is live-synced to the laptop and redundant to Dropbox cloud, so persistent memory survives a VPS disk loss. Inject the current user-local time (not VPS UTC) so the LLM doesn't mislabel "today" and "tomorrow." The three operational docs that still live in the workspace (`TOOLS.md`, `HEARTBEAT.md`, `CRONS.md`) are *not* loaded into the inbox system prompt — the `TOOLS` manifest the LLM sees is generated dynamically from `tools.py`, scheduled work is read from `fleet-manifest.json` + `ops/scripts/*-host.sh`, and heartbeat logic lives in `scripts/heartbeat.py`. The workspace copies of those files stay as operator-readable reference, not as LLM-loaded context.

4. **Typing indicator.** Fire `sendChatAction(typing)` so the operator sees the Telegram typing bubble while the LLM thinks.

5. **Conversation window.** Load the agent's sliding window (last 20 items, 1-hour inactivity timeout). Append the operator's message as a `{role: "user", content: ...}` item.

6. **Tool-use loop.** Call `tool_use.run()` with the assembled system prompt, the agent's `TOOLS` manifest, the agent's `EXECUTORS` dict, and the conversation window as `initial_items`. The loop calls the LLM, executes any tool the LLM picks, feeds the result back, and repeats until the LLM produces a text reply or the iteration cap (6) is hit.

7. **Pending-action button assembly.** After the loop, scan the tool outputs for `__pending_action__` markers. If found, build an `inline_keyboard` with per-action Confirm/Cancel buttons using the action IDs. If multiple actions were staged in one turn, group them into a batch and add "Confirm all N" / "Cancel all" buttons at the bottom.

8. **Persist + reply.** Append all new items (function calls, function call outputs, final assistant text) to the conversation window. Send the reply text + inline keyboard to Telegram.

### 3. The tool-use loop (`tool_use.py`)

A sync two-turn loop. Given a system prompt, a tool manifest, a dict of executors, and a list of input items, it:

1. Calls `agents.shared.llm.infer()` with the items + tools.
2. If the response is text — return it, done.
3. If the response is a `function_call` — execute the tool via the executor, append the echoed function call + the function call output to the running item list, and loop back to step 1.
4. Bounds the loop at 6 iterations so a runaway tool-picking model cannot spiral forever.

**Arguments as JSON strings, not dicts.** This is an empirical finding confirmed against the Codex Responses backend: when echoing a function call back to the model as an input item, the `arguments` field must be a JSON *string*, not a parsed dict. Sending a dict produces a schema validation error on the next `infer()` call. The loop handles this serialization internally.

**Timeout.** Each `infer()` call has a 90-second timeout. A tool-use turn that hangs (slow network, slow tool executor) is killed at the timeout boundary and returns an error to the caller.

**Exception handling on tool execution.** If a tool executor throws, the exception message is returned as the `function_call_output.output` string so the LLM can see what happened and either retry with different arguments or tell the operator something went wrong. No exception in a tool executor crashes the loop.

### 4. The conversation window (`conversation.py`)

Per-agent JSONL file at `~/.clawford/inbox/{agent-id}.jsonl`. Each line is a timestamped input item. The window is loaded by the dispatcher before every LLM call and trimmed by two rules:

- **Sliding window of 20 items.** Only the last 20 items are loaded, regardless of how many exist on disk. ~20 items is ~10 conversational turns, which is enough context for the LLM to follow a multi-step task without the input growing unbounded.
- **Inactivity timeout of 1 hour.** If the most recent entry in the log is older than 1 hour, `load()` returns an empty list — the LLM sees a fresh session. This prevents week-old context from leaking into a new conversation and producing confused responses.

**Append-only on disk.** New items are appended to the JSONL file, not rewritten. The file grows over time; the 20-item window is a read-side filter, not a write-side trim. The file is cheap to rotate if it gets large, but in practice a few weeks of casual operator chat is a few hundred KB.

**Robust to malformed lines.** Corrupted or partially-written JSONL lines are silently skipped on load. A power loss mid-write does not corrupt the conversation window.

### 5. The pending-actions store (`pending_actions.py`)

The unified staging system for every producer tool in the fleet. One JSON file per agent at `~/.clawford/{agent-id}-workspace/pending-actions.json`.

**Lifecycle:**
1. A producer tool (e.g., `propose_reorder`) calls `pending_actions.stage(agent_id, kind, payload, summary)`. The action gets a unique ID (`act_` + 12 hex chars), an expiry timestamp (default 4 hours from now), and customizable button labels.
2. The `stage()` call returns a dict with a `__pending_action__` marker. The dispatcher scans for this marker and auto-attaches inline buttons.
3. The operator taps Confirm — the dispatcher loads the action by ID, calls the agent's `confirm_{kind}` executor with the stored payload, and removes the action atomically on success.
4. The operator taps Cancel — the dispatcher removes the action and replies "Cancelled."
5. If neither button is tapped within the TTL window (4 hours), the action silently expires and is filtered out on the next `load()`.

**Thread safety.** Per-agent `threading.Lock` protects every read-modify-write cycle. The daemon is single-process, so Python locks suffice. The `remove()` call is double-tap safe — removing an already-removed action returns `None` instead of raising.

**Batch grouping.** When a single LLM turn stages multiple actions (e.g., "reorder both the water and the diapers"), the dispatcher assigns a shared `batch_id` and adds "Confirm all N" / "Cancel all" buttons alongside the per-item buttons.

## The tools.py pattern

Every agent defines its conversational surface in a single file: `agents/{agent-id}/tools.py`. The file exports two dictionaries:

**`TOOLS`** — a list of function schemas in the Codex Responses format. These are the tools the LLM can see and call. Each entry has a `name`, a `description` (which tells the LLM when to call it), and a `parameters` schema.

**`EXECUTORS`** — a dict mapping tool names to Python callables. Every name in `TOOLS` must have an entry in `EXECUTORS`. But `EXECUTORS` can also contain names that are **not** in `TOOLS` — these are the confirm executors, callable only via the dispatcher's callback shortcut path, never by the LLM.

Three kinds of tools:

| Kind | In TOOLS? | In EXECUTORS? | Side effects? | Example |
|------|-----------|---------------|---------------|---------|
| **Read tool** | yes | yes | none | `get_fleet_health`, `get_events_for_day`, `get_delivery_digest` |
| **Producer tool** | yes | yes | stages a pending action | `propose_reorder`, `propose_event_add` |
| **Confirm executor** | **no** | yes | executes the mutation | `confirm_reorder`, `confirm_calendar_add` |

The confirm executor is the piece that makes the pending-action flow safe. The LLM can call `propose_reorder`, which stages the action and returns a marker. The LLM **cannot** call `confirm_reorder` — that tool is not in the `TOOLS` manifest and the LLM does not know it exists. The only path to `confirm_reorder` is the operator tapping the Confirm button, which the dispatcher routes directly to the executor without touching the LLM.

### Per-agent tool inventory

| Agent | Read tools | Producer tools | Confirm executors |
|-------|-----------|---------------|-------------------|
| [Mr Fixit 🦊🔧](09-mr-fixit.md) | `get_fleet_health`, `get_morning_status`, `get_known_issues` | `propose_snooze_alert`, `propose_refresh_session`, `propose_rerun_cron`, `propose_remember` | `confirm_snooze_alert`, `confirm_refresh_session`, `confirm_rerun_cron`, `confirm_remember` |
| [Lowly Worm 🐛📰](10-lowly-worm-newsfeed.md) | `get_todays_digest`, `get_topic_weights`, `recent_engagements`, `ask_topic` | `record_engagement` (also via like/dislike/more buttons), `propose_remember` | `confirm_remember` |
| [Mistress Mouse 🐭📅](12-mistress-mouse.md) | `get_events_for_day`, `get_week`, `get_configured_calendars`, `get_recent_reminders_sent` | `propose_event_add`, `propose_event_move`, `propose_event_cancel`, `propose_remember` | `confirm_calendar_add`, `confirm_calendar_move`, `confirm_calendar_cancel`, `confirm_remember` |
| [Sergeant Murphy 🐷🔍](13-sergeant-murphy.md) | `get_meetings_for_day`, `get_week_meetings`, `get_commitment_status`, `get_coaching_config`, `get_recent_coaching_entries`, `force_prep`, `force_debrief` | `list_pending_action_items`, `confirm_action_item`, `dismiss_action_item`, `propose_coaching_toggle`, `propose_coaching_area_add`, `propose_coaching_area_remove`, `propose_remember` | `confirm_coaching_toggle`, `confirm_coaching_area_add`, `confirm_coaching_area_remove`, `confirm_remember` |
| [Huckle Cat 🐱🤝](14-huckle-cat.md) | `get_morning_nudge`, `get_upcoming_meetings`, `get_pending_triage`, `get_checkin_log`, `get_config_summary`, `get_person`, `get_commitments`, `force_nudge`, `force_triage`, `draft_reply` | `mark_checkin`, `snooze_reminder`, `dismiss_triage_n`, `propose_add_note`, `propose_add_person`, `propose_remember` | `confirm_add_note`, `confirm_add_person`, `confirm_remember` |

Every agent has `propose_remember` / `confirm_remember` — the self-learning memory surface added in the 2026-04 brain migration. Saying "from now on X" to any agent stages the rule with `[💾 Remember] [Skip]` inline buttons; tapping Remember appends to that agent's `MEMORY.md` (Dropbox-brain-synced), which is then loaded into every future system prompt.

Mr Fixit's three producer tools are the operational ones: `propose_snooze_alert` mutes a noisy alert for a specified window, `propose_refresh_session` kicks the relevant auth/session-refresh script (Krisp tokens, Google OAuth, etc.), `propose_rerun_cron` re-fires a cron that missed or errored. Each stages behind a confirm button for the same reason Huckle Cat's note flow does — these mutate shared state (fleet-health entries, session files, cron run-history) and I want the button in the loop. Huckle Cat now carries the richest tool surface — sixteen LLM-callable tools plus three confirm executors, covering relationship lookups (`get_person`, `get_commitments`), on-demand re-runs of the morning cron (`force_nudge`, `force_triage`), draft composition (`draft_reply`), note and person-file creation (`propose_add_note`, `propose_add_person`), and inbox triage dismissal (`dismiss_triage_n`).

## The inline button UX

**Single action.** When one producer tool fires in a turn:

```
🐭 Staged: Add "School pickup" to Thursday 3pm

[📅 Create event]  [Skip]
```

**Multiple actions (batch).** When the LLM stages two or more actions in one turn:

```
🐭 Staged 2 items:
• Thursday 3pm — School pickup
• Friday 6pm — Ballet recital

[📅 Create] School pickup   [Skip]
[📅 Create] Ballet recital  [Skip]
[✅ Confirm all 2]  [❌ Cancel all]
```

Per-item buttons use individual action IDs. Batch buttons use a shared `batch_id`. "Confirm all" iterates over every action in the batch and calls the per-kind confirm executor for each.

**Engagement buttons.** [Lowly Worm's](10-lowly-worm-newsfeed.md) morning edition articles can carry `[👍 Like]` `[👎 Dislike]` `[📖 More]` buttons. These are hardwired callback shortcuts (`like:{article_id}`, `dislike:{article_id}`, `more:{article_id}`) that route directly to the `record_engagement` executor without going through the pending-action store — they are instant feedback, not staged mutations.

**Button label customization.** The `stage()` call accepts `confirm_label` and `cancel_label` parameters. Mistress Mouse's calendar tools use `"📅 Create event"` / `"Cancel"`. The default is `"✅ Confirm"` / `"❌ Cancel"`.

## Deployment walkthrough

The inbox daemon runs alongside the host-cron runtime — crons handle the scheduled outbound work, the daemon handles the conversational inbound work. Both run on the same VPS under the same user.

**Step 1: Verify bot tokens.** Every agent needs its own Telegram bot token in `~/clawford/.env`. The daemon resolves tokens via a per-agent candidate list (e.g., Mr Fixit tries `FIXIT_BOT_TOKEN` then falls back to `TELEGRAM_BOT_TOKEN` for historical reasons). Verify all six tokens are present.

**Step 2: Deploy tools.py files.** `python3 agents/shared/deploy.py <agent>` for each agent that has a `tools.py`. The deploy tool copies the `tools.py` into the agent's workspace alongside the rest of the scripts.

**Step 3: Install the systemd user unit.** Run `ops/scripts/install-inbox-systemd.sh` on the VPS. The script copies `ops/systemd/clawford-inbox.service` to `~/.config/systemd/user/`, runs `daemon-reload`, enables the service, and starts it. If a `nohup`-launched daemon is already running, the script gracefully kills it first.

**Step 4: Verify.** `systemctl --user status clawford-inbox` should show `active (running)`. Message one of the bots on Telegram — Mr Fixit is a good first test: ask "how's the fleet?" and verify the response includes real fleet-health data.

**Step 5: The kill-switch toggle.** To run the daemon locally for development, create `~/.clawford/inbox-disabled` on the VPS (`touch ~/.clawford/inbox-disabled`), then `systemctl --user stop clawford-inbox`. The `ExecStartPre` check in the unit file prevents the service from restarting while the kill-switch exists. Run the daemon locally on the laptop, do the development, then remove the file on the VPS and `systemctl --user start clawford-inbox` to re-enable.

## Real-time Gmail triage via Pub/Sub pull

The Telegram dispatcher is one inbound channel. Gmail is the other. The 30-minute `inbox-triage` cron described in [Ch 14 — Huckle Cat](14-huckle-cat.md#the-correspondence-layer) gave the fleet half-hour-worst-case latency on inbound mail, which meant time-sensitive threads — *"can you call in ten minutes?"* — landed in the operator's eyes before they landed in the triage queue. A parallel systemd service runs on the VPS to close that gap.

The same architectural stance governs both channels: **pull, not push.** The VPS has no public HTTPS endpoint, and adding one to receive Gmail webhooks would have been a new attack surface for one feature. Google Cloud Pub/Sub supports a pull subscription model where the consumer holds an outbound long-poll connection to Google — identical latency to push, no public port required, no TLS termination, no webhook signature validation.

**The pieces.**

- A GCP Pub/Sub topic (`gmail-huckle-inbox`) and pull subscription (`huckle-gmail-pull`) in the same project that holds the Gmail OAuth credentials. Gmail's managed service agent (`gmail-api-push@system.gserviceaccount.com`) gets `roles/pubsub.publisher` on the topic.
- A daily cron (`gmail-watch-renew` at `0 7 * * *` UTC) that calls Gmail's `users.watch()` to (re-)subscribe the mailbox to the topic. Gmail expires watches after seven days regardless of what the response says, so a daily renewal gives six days of slack.
- A persistent systemd service (`clawford-huckle-push`) that long-polls the pull subscription, walks the Gmail history delta on each notification, and fires `inbox-triage --thread-id <id>` followed by `auto-compose --force <id>` per new thread as subprocesses. Triage and compose reuse the exact code paths the polling cron runs — the listener only adds a real-time trigger.
- A rolling cursor file (`cache/gmail-history-cursor.json`) so the listener can pick up where it left off across restarts. On first boot (or after a cursor wipe), the listener seeds itself from the renewal cron's `gmail-watch-state.json`.

**Why the cursor matters.** Pub/Sub notifications only carry a historyId, not the thread itself. The listener has to call `users.history.list(startHistoryId=<cursor>, labelId=INBOX, historyTypes=["messageAdded"])` to discover which threads actually changed. The cursor advances on every successful tick even when no threads matched — the alternative is replaying the same delta forever, which burns quota and doesn't make anyone faster.

**Belt-and-suspenders by design.** The 30-minute `inbox-triage` and `:05/:35 auto-compose` crons stay enabled. If the listener crashes, if the watch expires, if Pub/Sub has an edge case — the polling path catches anything missed. Gmail's history API is idempotent on thread ID and `auto-compose-log.json` dedupes on the same key, so double-processing is cheap and invisible.

**The first persistent agent-owned daemon outside the Telegram dispatcher.** That's not a small change to the fleet's shape. Every other agent runs as a cron, exits, and leaves no long-lived process. The push listener runs under `User=openclaw` (the same identity the host-cron wrappers use, so it can read the SCP'd token) with `Restart=on-failure` and a file-based kill switch at `~/.clawford/huckle-push-disabled`. The heartbeat probe in `heartbeat.py` checks two signals: `systemctl is-active clawford-huckle-push` (the service is running) and `gmail-watch-state.json` expiration >36h out (the renewal cron is healthy). Either failing pings Telegram; the Telegram alert lead-time plus the six-day expiration slack means a gap requires three consecutive renewals failing silently — unlikely, but if it happens the polling cron is still delivering drafts.

> 🔦 **Tip.** The listener's log at `~/.clawford/logs/huckle-push.log` emits one line per non-trivial tick: `[listener] tick: {"pulled": 1, "threads_processed": 1, ...}`. Empty ticks (long-poll timeout, nothing in the subscription) don't log — otherwise the log would grow by a dozen lines per minute for no signal. Grep `pulled` to see inbound volume; grep `errored` to catch subprocess failures.

> 🧨 **Pitfall.** OAuth consent screen silently strips unverified scopes. **Why:** adding the `pubsub` scope to `gmail-auth.py`'s `SCOPES` list is necessary but not sufficient. If the GCP OAuth consent screen doesn't list `https://www.googleapis.com/auth/pubsub` as an approved scope for the app, Google's authorization flow silently drops it from the returned token — the user clicks through, the token lands on disk, and the first `subscriptions().pull()` call fails with a 403 error that looks like a coding bug. **How to avoid:** after adding any new scope to an auth script, verify the scope also appears on the consent screen's Data Access page *before* re-running the flow. A `python -c "import json; print(json.load(open('token.json'))['scopes'])"` sanity check on the new token catches the same case one step later.

> 🧨 **Pitfall.** Running the listener as `root` when the token lives under `/home/openclaw/`. **Why:** systemd services default to running as root if `User=` is not set. The listener's default paths expand `~/` against the service's own home — `/root` — and the token isn't there. The first boot reports `token.json not found at /root/.clawford/connector-workspace/token.json` and the service flaps into a restart loop. **How to avoid:** the unit file sets `User=openclaw` and `Group=openclaw` explicitly. Don't drop those when copying the unit to a new service; the same bug will bite the next persistent agent-owned daemon.

## The locked-in decisions

Nine architectural decisions that are load-bearing and documented here so future changes do not accidentally undo them.

1. **Long-polling, not webhooks — on every inbound channel.** Webhooks require a public HTTPS endpoint on the VPS. The VPS has no public HTTP services and adding one is a security surface expansion. Telegram long-polling works from behind any firewall. Pub/Sub pull works identically for the Gmail ingestion path. Same principle, two implementations.

2. **Codex Responses API with native `tools` field.** Not prompt-based tool dispatch. The model sees a structured tool manifest and returns structured `function_call` responses. `store=false` is mandatory — the Codex backend does not support server-side conversation storage. Empirically verified; `previous_response_id` chaining does not work.

3. **Two-turn tool-use loop, not multi-turn streaming.** The current 2–4 second model response time is under the 5-second threshold where streaming edits (progressive message updates) would meaningfully improve UX. If model latency increases, streaming edits become the natural next step.

4. **k=20 sliding window + 1-hour inactivity timeout.** Entirely client-side conversation state. The VPS cannot use `previous_response_id` chaining (decision 2), so the client manages its own window. 20 items is ~10 turns, which is enough for multi-step tasks. The 1-hour timeout prevents stale context from leaking.

5. **Single `TELEGRAM_CHAT_ID` gate.** No multi-user support. A Clawford fleet is a single-operator system and the inbox daemon reflects that. Adding multi-user support would require session partitioning, per-user conversation windows, and a role-based access model — none of which exists today.

6. **`sendChatAction(typing)` + atomic reply.** The operator sees a typing indicator while the LLM thinks, then receives one complete message. No streaming edits, no partial replies, no progressive rendering. Simple and predictable.

7. **Kill-switch file for dev-vs-VPS toggle.** `~/.clawford/inbox-disabled` exists → daemon exits cleanly. The systemd unit's `ExecStartPre` check prevents restarts while the file exists. This avoids Telegram's single-poller-per-bot lock collision when the operator runs the daemon locally.

8. **Silent-failure discipline.** Every subprocess invocation in a tool executor goes through `agents.shared.subprocess_helpers.run_json_script`, which returns a structured error dict on failure instead of raising. The tool-use loop catches exceptions in tool executors and returns the error message as the function call output so the LLM can report the failure conversationally.

9. **Bot-token resolution via per-agent candidate list.** Each agent has a list of env var names to try (e.g., `["FIXIT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"]`). First match wins. This supports both the new per-agent token names and historical fallbacks without forcing a rename.

## Pitfalls

> 🧨 **Pitfall.** Kill-switch file left behind after a local dev session. **Why:** the operator creates `~/.clawford/inbox-disabled` on the VPS to park the production daemon while testing locally, finishes the session, forgets to remove the file, and the production daemon stays parked indefinitely. The fleet's outbound crons keep working (they don't check the kill-switch), but no inbound messages are processed. The operator only notices when they message a bot and get no reply. **How to avoid:** the systemd unit logs "inbox kill-switch is active, exiting" to `~/.clawford/logs/inbox.log` on every failed start attempt. `grep -c 'kill-switch' ~/.clawford/logs/inbox.log` is a quick check. The fleet-health probe does not currently check for kill-switch presence — adding that probe is a future improvement.

> 🧨 **Pitfall.** Running the inbox daemon locally while the VPS daemon is still active. **Why:** Telegram enforces a single long-poller per bot token. If two processes poll the same token concurrently, both receive intermittent `409 Conflict` errors and both drop messages non-deterministically. The symptom is that some messages get responses and some silently vanish. **How to avoid:** always create the kill-switch file on the VPS and stop the systemd service *before* starting the daemon locally. Check with `systemctl --user status clawford-inbox` — if it says `active (running)`, the VPS daemon is still polling.

> 🧨 **Pitfall.** Adding a tool to `TOOLS` but forgetting the executor in `EXECUTORS`. **Why:** the LLM will call the tool, the tool-use loop will look up the executor, find nothing, and return `"ERROR: unknown tool '{name}'"` as the function call output. The LLM will then apologize to the operator with a generic error message. The operator sees "something went wrong" with no actionable detail. **How to avoid:** every entry in the `TOOLS` list must have a matching entry in the `EXECUTORS` dict. Check both dicts before committing a new tool. The test suite for each agent's `tools.py` asserts `set(t["name"] for t in TOOLS) <= set(EXECUTORS.keys())`.

> 🧨 **Pitfall.** Adding a confirm executor to `TOOLS` (making it LLM-callable). **Why:** the entire point of the pending-action flow is that mutations require a human button tap. If `confirm_reorder` is in the `TOOLS` manifest, the LLM can call it directly, bypassing the inline-button gate. The operator asks "reorder the water" and the LLM adds it to the cart immediately without asking for confirmation. **How to avoid:** confirm executors go in `EXECUTORS` only, never in `TOOLS`. The naming convention is the guard: anything named `confirm_{kind}` should trigger a code-review reflex to verify it is not in the `TOOLS` list. The test suite asserts `confirm_` prefixed names are not in `TOOLS`.

> 🧨 **Pitfall.** Pending-action TTL silently expiring before the operator sees the buttons. **Why:** the default TTL is 4 hours. If the operator asks an agent to stage an action at 10 AM and doesn't check Telegram until 3 PM, the action has expired and the buttons do nothing — tapping Confirm returns "action not found." The operator has to re-request. **How to avoid:** 4 hours is a reasonable default for an operator who checks Telegram a few times a day. If the operator's cadence is longer, increase the `ttl_hours` parameter in the `stage()` call for that agent's producer tools. There is no fleet-wide config for TTL — it is per-tool.

> 🧨 **Pitfall.** Missing `__pending_action__` marker in a producer tool's return value. **Why:** the dispatcher scans tool outputs for `__pending_action__` to decide whether to attach inline buttons. If a producer tool calls `pending_actions.stage()` but discards the return value and returns something else, the action gets staged in the JSON file but no buttons appear in the Telegram message. The operator sees a text reply that says "staged a reorder" but has no way to confirm it — they have to wait for TTL expiry or manually remove the action. **How to avoid:** every producer tool must `return pending_actions.stage(...)` — the `stage()` return value IS the tool's return value. Do not post-process it, do not wrap it, do not replace it.

> 🧨 **Pitfall.** Bot token env var missing from `~/clawford/.env` on the VPS. **Why:** the daemon reads all six bot tokens from the env file. A missing token means that agent's poll task starts with `token=None`, the first `getUpdates` call fails, the task enters the 5-second backoff loop, and it retries forever — logging an error on every attempt but never crashing (per the per-update error isolation design). Meanwhile, the other five agents work fine and the operator might not notice the sixth is unreachable for days. **How to avoid:** `install-inbox-systemd.sh` checks for the env file at install time. After adding a new agent's bot token, restart the service (`systemctl --user restart clawford-inbox`) and verify all six tasks are polling cleanly in the first 30 seconds of `~/.clawford/logs/inbox.log`.

> 🧨 **Pitfall.** `callback_data` format mismatch between the dispatcher and the button builder. **Why:** the dispatcher parses callback data by string prefix (`confirm:`, `cancel:`, `confirm_all:`, etc.). If a button is built with a slightly different format — `confirm_` instead of `confirm:`, or extra whitespace, or a JSON-encoded payload instead of a bare ID — the prefix check fails and the callback falls through to the LLM path, which has no idea what to do with it. **How to avoid:** all callback data is built by the dispatcher's button-assembly code, not by the tool itself. Tools return `__pending_action__` markers; the dispatcher builds the buttons from those markers using a consistent `f"confirm:{action_id}"` template. If you add a new callback shortcut type, add it to both the button-builder and the callback-parser in the same commit.

## See also

- [Ch 06 — Infra setup](06-infra-setup.md) — the host-cron runtime + shared library that the inbox daemon sits alongside
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the outbound side of the agent story
- [Ch 17 — Auth architectures](17-auth-architectures.md) — the bot token is Shape 3 (bearer token in `.env`)
- [Ch 19 — Security and hardening](19-security-and-hardening.md) — the chat_id gate and the inbox daemon's attack surface
- [Ch 20 — Scripts and configs](20-scripts-and-configs.md) — the file-level reference for the five shared modules
