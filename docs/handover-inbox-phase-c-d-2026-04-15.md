# HANDOVER: Clawford inbox dispatcher — Phases C & D

**Context window is running out. This doc captures everything the previous session shipped so a fresh Claude can pick up Phases C and D without replaying the whole arc.**

Last commit on master when this was written: `bfd9908` (`deploy: add subprocess_helpers.py to shared library allowlist`).

Read this file first. Then `CHANGELOG.md` + the last ~5 commits. Then start with Phase C.

---

## TL;DR

the operator's six Busytown agents (Mr Fixit, Lowly Worm, Hilda Hippo, Mistress Mouse, Sergeant Murphy, Huckle Cat) now talk back on Telegram via an async long-polling daemon at `agents/shared/telegram_inbox.py`. Each inbound message → dispatcher → tool-use loop against codex Responses API → reply. Phase A (infrastructure + Mr Fixit) and Phase B (read tools for all five remaining agents) are shipped and verified end-to-end. A silent-failure audit swept 12 scripts onto a shared `run_json_script` helper with tagged-error propagation. Google OAuth re-authed, LinkedIn chromium path fixed, calendar timezone aware.

**Still ahead:**

- **Phase C** — producer tools (propose/confirm/cancel) with inline-keyboard buttons on every staged action. Hilda reorders, Mouse calendar changes, Murphy action items, Worm engagement taps, Huckle check-ins.
- **Phase D** — systemd user unit for the daemon with kill-switch file and auto-restart.

---

## What's live on the VPS RIGHT NOW

- **Daemon**: `python3 agents/shared/telegram_inbox.py` running under nohup (NOT yet systemd — that's Phase D). Started via a sequence like:
  ```
  ssh openclaw@203.0.113.10 "nohup bash -c 'set -a; source ~/clawford/.env; set +a; cd ~/repo && exec python3 agents/shared/telegram_inbox.py' > ~/.clawford/logs/inbox.log 2>&1 & disown"
  ```
  Check: `ps -ef | grep telegram_inbox | grep -v grep`
  Logs: `~/.clawford/logs/inbox.log` (httpx long-poll lines every 25s per bot)

- **Six concurrent poll tasks** inside one async process, one per bot. All six bot tokens are in `~/clawford/.env` and the daemon reads them on startup. Per-agent offsets persist at `~/.clawford/inbox/<agent>.offset`.

- **Conversation windows** at `~/.clawford/inbox/<agent>.jsonl` (k=20 sliding window, 1h inactivity timeout).

- **All six agents have a `tools.py`** at `agents/<agent>/tools.py`:
  - **fix-it**: `get_fleet_health`, `get_morning_status`, `get_known_issues`
  - **news-digest**: `get_todays_digest`, `get_topic_weights`, `recent_engagements`
  - **shopping**: `get_delivery_digest`, `get_recent_orders`, `get_grocery_list`, `get_pending_actions`, `find_amazon_item`, `find_costco_item`
  - **family-calendar**: `get_events_for_day`, `get_week`, `get_configured_calendars`, `get_recent_reminders_sent`
  - **meetings-coach**: `get_meetings_for_day`, `get_week_meetings`, `get_commitment_status`, `get_coaching_config`, `get_recent_coaching_entries`
  - **connector**: `get_morning_nudge`, `get_upcoming_meetings`, `get_pending_triage`, `get_checkin_log`, `get_config_summary`

- **Mouse/Murphy boundary enforced at the tool layer**:
  - Mouse's `get_events_for_day` / `get_week` call `gcal-fetch.py --skip-meetings` → events with Workflowy links go to Murphy only.
  - Murphy's `get_meetings_for_day` / `get_week_meetings` filter `is_real_meeting=True` from the gcal-fetch output → non-meetings (Pick up meds, Dentist) stay in Mouse's domain.

- **Google OAuth fresh** (re-authed 2026-04-16 00:11 UTC via `gcal-auth.py` run locally on the operator's Windows machine, token SCP'd to both `~/.clawford/family-calendar-workspace/token.json` and `~/.clawford/meetings-coach-workspace/token.json`). Google tokens expire ~every 6 months if unused; expect another re-auth around 2026-10.

- **Heartbeat probes now actually exercise credentials** via `get_credentials()` in both calendar agents' `heartbeat.py`. Returns `'revoked'` on `invalid_grant`, `'error'` on other exceptions. The 2026-04-15 incident (2.5 days of silent outage) cannot recur at the agent level.

- **LinkedIn keepalive** works: `playwright_profile.py` no longer forces `executable_path="/usr/bin/chromium"`; Playwright's bundled chromium at `~/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome` is used by default.

- **Calendar tools are timezone-aware**: both Mouse and Murphy read `timezone` from their respective config files (`calendar-config.json` / `meeting-config.json`) and compute "today" / "tomorrow" in the operator's wall clock (America/Los_Angeles). The dispatcher also injects the current user-local time into every system prompt as a `# Current context` section so the LLM interprets relative dates correctly.

- **Silent-failure audit shipped**: all 12 agent scripts that used `_run_script` now import from `agents/shared/subprocess_helpers.py::run_json_script`, which returns `{"__error__": "<reason>"}` on any subprocess failure. Callers check `is_subprocess_error()` and propagate to `status: error` with a per-agent-emoji alert. Adversarial test verified — simulating a missing check script now fires a Telegram alert within one cron cycle.

---

## Architectural decisions locked in — DO NOT re-litigate

The previous session went through multiple rounds of research, empirical probes, and user sign-off on these. They are load-bearing:

1. **Runtime = long-polling daemon, single async process, 6 concurrent poll tasks, shared httpx client.** Not systemd yet. Not webhooks. Long-poll with `timeout=25` gives ~100ms inbound latency without exposing public ports.

2. **LLM = codex Responses API with native `tools` field.** Empirically verified this works via direct probe against `https://chatgpt.com/backend-api/codex/responses`. The endpoint rejects `store: true` — every call must be stateless from the server's side. Conversation state is entirely client-side in `agents/shared/conversation.py`. Do not try to use `previous_response_id` chains — they return 400.

3. **Tool-use loop = two turns.** `agents/shared/tool_use.py::run()` calls `llm.infer` with tools, gets back either text OR a function_call, executes the tool via the `executors` dict, feeds the result back as `function_call_output`, calls infer again, returns composed text. Max iterations 6. Tool exceptions are caught and fed to the model as "ERROR: ..." so it can apologize or retry.

4. **Conversation state = k=20 sliding window + 1h inactivity timeout + JSONL persistence.** Per-agent files at `~/.clawford/inbox/<agent>.jsonl`. Persistence is append-only. Malformed lines are silently skipped on load.

5. **Security gate = single chat_id filter.** Dispatcher drops any update where `effective_chat.id != 111111111` before touching tools or LLM. Do not add multi-user support.

6. **UX polish = `sendChatAction(typing)` + atomic reply.** No `editMessageText` streaming. Research confirmed that streaming edits shine only for >5s responses; our typical 2-4s tool-use loop is under that threshold.

7. **Dev-vs-VPS toggle = kill-switch file.** `~/.clawford/inbox-disabled` → daemon exits cleanly at startup. `touch` to disable, `rm` to re-enable. This is the pattern for running the daemon locally against the same production bot tokens without Telegram's single-poller-per-bot lock colliding.

8. **Silent-failure discipline = every subprocess call goes through `run_json_script`.** If a new script adds a `_run_script` local function, that's a bug — migrate it to the shared helper.

9. **Bot token resolution = per-agent candidate list with first-match-wins.** `dispatcher.py::AGENT_TOKEN_ENV` maps each agent to a list of candidate env var names. Fix-it tries `FIXIT_BOT_TOKEN` then falls back to `TELEGRAM_BOT_TOKEN` (the VPS historical name). Do not force a rename.

---

## What was shipped in the previous session

### Phase A — Infrastructure + Mr Fixit end-to-end (commit `4758d6a` + fixups)

- **`agents/shared/llm.py`** — extended `infer()` with `tools` parameter and `input_items` parameter. Parses `response.function_call_arguments.done` events from SSE stream into `InferResult.function_call = {call_id, name, arguments (dict)}`. Added `response_id` capture for debug traceability. Mutual-exclusive validation on `prompt` vs `input_items`.
- **`agents/shared/tool_use.py`** — new. Two-turn loop with max_iters guard. Echoes function_call items with arguments serialized as JSON strings (required by the codex backend — confirmed empirically). Tool executor exceptions surface as structured error outputs to the model.
- **`agents/shared/conversation.py`** — new. Per-agent sliding window with JSONL persistence, inactivity timeout, robust to malformed lines.
- **`agents/shared/telegram_api.py`** — added `send_chat_action()` for typing indicator.
- **`agents/shared/dispatcher.py`** — new. One entry point: `dispatch(agent_id, update)`. Chat ID gate → typing indicator → load conversation window → build system prompt (SOUL.md + USER.md + MEMORY.md + tool doc + current user-local time) → run tool_use → persist → send reply.
- **`agents/shared/telegram_inbox.py`** — new. Async main with 6 concurrent `poll_loop` tasks, shared httpx client, kill-switch file check, per-update error isolation, backoff on getUpdates failures.
- **`agents/fix-it/tools.py`** — new. Three read tools: `get_fleet_health`, `get_morning_status`, `get_known_issues`.
- **Tests**: `test_llm_tools.py` (new tests in existing file), `test_tool_use.py`, `test_conversation.py`, `test_dispatcher.py`, `test_telegram_inbox.py`. All green.
- **Commit `24cb2cd`**: fallback to `TELEGRAM_BOT_TOKEN` for fix-it (VPS historical name).
- **Smoke test**: message "how's the fleet?" → `get_fleet_health` tool → reply "✅ All clear. 6 agents healthy. Latest snapshot 10 min old."

### Phase B — tools.py for 5 remaining agents (commit `a1bb6b0`)

Read-only tools wrapping each agent's cached state and scripts. See "What's live on the VPS" above for the per-agent tool list.

Notable: Hilda has `find_amazon_item(query)` and `find_costco_item(query)` which subprocess the existing `amazon-reorder.py find-item` / `costco-reorder.py find-item` entry points. **These existing scripts are LLM-friendly** — they have clean `find-item` / `add-to-cart --confirm` subcommands designed for chaining. Phase C can rely on this.

### Bug triage round 1 (commits `6a4f78e`, `976ab18`, `cd6172e`)

- **Fleet-health cron staggered** `2,17,32,47 * * * *` so it never collides with costco-token-refresh `*/5` (prior incident: 2.5 false alerts/day from transient races).
- **heartbeat.py self-heal exception logging** for shopping: `cache/heartbeat-selfheal.log` JSONL.
- **Google OAuth re-auth** (the operator ran `gcal-auth.py` locally, session SCP'd the token up).
- **Family-calendar + meetings-coach heartbeat probes** now call `get_credentials()` to actually test the refresh round-trip. Detects `invalid_grant` → bucket `revoked`.
- **Mouse/Murphy boundary fix** (see above).
- **Calendar TZ fix** (see above).
- **LinkedIn chromium path fix**: `playwright_profile.py` no longer hardcodes `/usr/bin/chromium`.

### Silent-failure audit sweep (commits `9e4c3ed`, `bfd9908`)

- **`agents/shared/subprocess_helpers.py`** — new. `run_json_script(path, *args, timeout)` → parsed JSON OR `{"__error__": "<reason>"}`. `is_subprocess_error(result)` predicate. 13 tests.
- **12 scripts migrated**: gmail-invite-alert, activity-email-alert, whatsapp-chat-alert, morning-briefing, whatsapp-schedule-post, delivery-digest, notes-triage-alert, morning-relationship-nudge, pre-meeting-alert, post-meeting-scan, morning-meeting-brief, commitment-follow-up. Each run() updated to check `is_subprocess_error()` and propagate `status: error` with a per-agent-emoji alert.
- **fetch-and-rank.py** now reports `status: degraded` when any feed source fails (was always `ok`).
- **shopping/heartbeat.py `_decode_remaining_ttl`** now logs to `cache/heartbeat-selfheal.log` on unparseable token (was silently returning -1).
- **`deploy.py::SHARED_RUNTIME_MODULES`** now includes `subprocess_helpers.py` so the deploy sync mirrors it into each agent workspace.
- **1197 tests green** across shared + six agent trees.

---

## Phase C — producer tools + inline buttons (YOUR FIRST TASK)

**Goal**: let each agent actually *do* things, not just report. User drives everything conversationally; tools stage actions; user confirms via inline button or `/confirm <id>`.

### Architectural proposal — the "pending action" pattern

Unified across all agents. Don't reinvent per-agent:

1. **Tool stages an action**: `propose_reorder(source, item_id, quantity)` writes a JSON entry to `~/.clawford/<agent>-workspace/pending-actions.json` (already exists for shopping; create for others) with shape:
   ```json
   {
     "updated_at": "...",
     "actions": [
       {
         "id": "act_abc123",
         "kind": "reorder",
         "agent": "shopping",
         "staged_at": "...",
         "expires_at": "... + 24h",
         "summary": "Reorder 1x Kirkland Signature Spring Water 40pk from Costco",
         "payload": {"source": "costco", "item_number": "1914462", "quantity": 1}
       }
     ]
   }
   ```
   The tool returns `{"action_id": "act_abc123", "summary": "..."}` to the LLM, plus a structured `__pending_action__` marker the dispatcher can intercept.

2. **Dispatcher auto-attaches inline buttons**: after tool_use.run() returns, dispatcher scans `new_items` for any tool output whose parsed JSON contains a `__pending_action__` with `id`. If found, it attaches an `inline_keyboard` with `[✅ Confirm] [❌ Cancel]` buttons whose `callback_data` is `confirm:act_abc123` / `cancel:act_abc123`. Every staged action gets the same UX.

3. **Callback handling**: dispatcher's `_extract_user_text` already routes `callback_query` events as `[callback: confirm:act_abc123]`. Extend to parse `confirm:<id>` / `cancel:<id>` prefixes and synthesize a tool call directly: `confirm_pending(id=act_abc123)` or `cancel_pending(id=act_abc123)`. The corresponding tool executes or discards the staged action. This way the LLM can also honor a typed `/confirm act_abc123` since it goes through the same tool.

4. **Expiry**: pending actions auto-expire after 24h (or whatever you pick). A new cron `*/30 * * * *` called `pending-actions-prune-host.sh` sweeps expired entries. Alternative: check on every read.

5. **Every button guaranteed to have a handler**: the test suite must assert that for every `kind: <x>` a pending action can carry, there's a matching `confirm_<x>` and `cancel_<x>` in the agent's executors dict. No orphan buttons.

### Per-agent producer tools to build

**Hilda Hippo (shopping) — highest-value agent for this phase**
- `propose_reorder(source, item_id, quantity)` — stages a pending-action with `kind: reorder`
- `confirm_reorder(action_id)` — reads pending, subprocesses `costco-reorder.py add-to-cart --item-number X --confirm` OR `amazon-reorder.py add-to-cart --asin X --confirm`, removes from pending
- `cancel_reorder(action_id)` — removes from pending
- `add_to_grocery(item_name)` — appends to grocery-list.json (no pending — direct action, trivial)
- `remove_from_grocery(item_name)` — same

**Mistress Mouse (family-calendar)**
- `propose_event_add(calendar_id, who, what, when)` — stages pending-calendar-change
- `propose_event_move(event_query, new_start)` — stages
- `propose_event_cancel(event_query)` — stages
- `confirm_calendar_change(action_id)` — reads pending, subprocesses `gcal-write.py` (exists at `agents/family-calendar/scripts/gcal-write.py`), removes from pending
- `cancel_calendar_change(action_id)` — removes

**Sergeant Murphy (meetings-coach)**
- `list_pending_action_items()` — reads `cache/pending-debrief-*.json` files, returns all open action items
- `confirm_action_item(item_id)` — marks item as accepted; if config says so, write to Workflowy via the existing workflowy-sync.py
- `dismiss_action_item(item_id)` — marks dismissed

**Lowly Worm (news-digest)**
- `record_engagement(article_id, action)` — appends directly to `preferences/engagement.jsonl`. Action ∈ {thumbs_up, thumbs_down, more}
- Plus: the dispatcher's callback_query path should handle `like:N` / `dislike:N` / `more:N` callback_data (existing format from morning-fleet-deliver.py). Route to this tool. The engagement-poller cron can stay running as a belt-and-braces scraper — leave it alone.

**Huckle Cat (connector)**
- `mark_checkin(person_name)` — records a manual check-in in `checkin-log.json`
- `snooze_reminder(person_name, days)` — pushes the next overdue reminder out by N days
- Probably nothing else for v1.

### Phase C file plan

```
agents/shared/dispatcher.py
    + _parse_callback_data()  # handles confirm:id / cancel:id / like:N
    + _attach_pending_action_buttons(reply_text, new_items)  # post-tool_use
    update send_message call to pass reply_markup when present

agents/shared/pending_actions.py  (NEW)
    + stage(agent, kind, payload, summary, ttl_hours=24) -> action_id
    + load(agent) -> list of pending actions
    + load_by_id(agent, action_id) -> action or None
    + remove(agent, action_id) -> bool
    + prune_expired(agent) -> int
    uses JSONL for atomic append + rewrite-on-remove

agents/<each>/tools.py
    extend with producer tools (see per-agent list above)

ops/scripts/pending-actions-prune-host.sh  (NEW cron)
    calls agents/shared/pending_actions.prune_expired() for all agents

agents/shared/tests/test_pending_actions.py  (NEW)
agents/shared/tests/test_dispatcher_callback.py  (NEW — extend dispatcher tests)
agents/<each>/tests/test_producer_tools.py  (NEW per-agent)
```

### Phase C open questions to resolve with the operator

1. **Action expiry TTL**: 24h reasonable default, or shorter (e.g. 1h) so stale proposals don't linger?
2. **Confirm button style**: `[✅ Confirm] [❌ Cancel]` or more descriptive per action (`[🛒 Buy now] [Skip]`)?
3. **Multi-step confirmation**: if the user asks "reorder everything I've bought in the last 2 weeks", the LLM stages 5 actions. Do we emit 5 separate messages with 5 button pairs, or one consolidated message with `[Confirm all] [Review] [Cancel all]`?
4. **Workflowy integration for Murphy**: should `confirm_action_item` actually write to Workflowy, or just mark the pending-debrief entry? Depends on how the existing workflowy-sync.py is structured.
5. **Callback_query security**: the current dispatcher gate is on `chat_id`. Callback queries have their own `from.id` — confirm it's also the operator's, not just the originating message's chat.

### Phase C success criteria

- Ask Hilda "reorder the Kirkland water" → she finds it → stages a pending action → sends a message with `[Confirm] [Cancel]` buttons → tapping Confirm executes the real Costco cart-add → Hilda replies "Added to cart. Go check out when ready."
- Same round-trip for Mouse "move the dentist to Friday at 3pm".
- Same for Murphy "confirm the Yesol action item about Q2 roadmap".
- Tap a 👍 in the morning Lowly Worm digest → engagement.jsonl gets a new row immediately (no waiting for engagement-poller cron).
- TDD every producer tool before deploying. TDD the dispatcher's callback-parse + button-attach paths.

---

## Phase D — systemd unit + kill switch (YOUR SECOND TASK)

### Systemd user unit

`ops/systemd/clawford-inbox.service` (NEW file):

```
[Unit]
Description=Clawford Telegram inbox dispatcher daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/home/openclaw/clawford/.env
ExecStartPre=/usr/bin/test ! -f /home/openclaw/.clawford/inbox-disabled
ExecStart=/usr/bin/python3 /home/openclaw/repo/agents/shared/telegram_inbox.py
WorkingDirectory=/home/openclaw/repo
Restart=on-failure
RestartSec=5
StandardOutput=append:/home/openclaw/.clawford/logs/inbox.log
StandardError=append:/home/openclaw/.clawford/logs/inbox.log

[Install]
WantedBy=default.target
```

`ExecStartPre` exits non-zero if the kill-switch file exists → systemd refuses to start. `Restart=on-failure` + `RestartSec=5` gives durable restart.

### Install script

`ops/scripts/install-inbox-systemd.sh` (NEW):

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO=/home/openclaw/repo
UNIT_SRC=$REPO/ops/systemd/clawford-inbox.service
UNIT_DEST=$HOME/.config/systemd/user/clawford-inbox.service

mkdir -p "$(dirname "$UNIT_DEST")"
cp "$UNIT_SRC" "$UNIT_DEST"

systemctl --user daemon-reload
systemctl --user enable clawford-inbox.service

# Graceful cutover: stop any nohup-launched daemon first
pkill -f 'agents/shared/telegram_inbox.py' || true
sleep 1

systemctl --user start clawford-inbox.service
sleep 2
systemctl --user status clawford-inbox.service --no-pager
```

### Tests

- **test_clawford_inbox_service**: parse the unit file, assert required keys
  (`ExecStart`, `EnvironmentFile`, `Restart=on-failure`, kill-switch check)
- **Manual smoke test** (runbook for the session):
  1. `bash ops/scripts/install-inbox-systemd.sh` on the VPS
  2. `systemctl --user status clawford-inbox` → active (running)
  3. `pkill -9 -f telegram_inbox.py` → daemon gets killed
  4. Wait 10s, `systemctl --user status clawford-inbox` → active (running) again (restarted)
  5. `touch ~/.clawford/inbox-disabled && systemctl --user restart clawford-inbox` → status: failed (ExecStartPre blocked)
  6. `rm ~/.clawford/inbox-disabled && systemctl --user restart clawford-inbox` → active (running) again
  7. Message a bot from Telegram → reply arrives normally

### Phase D gotchas

- `systemctl --user` requires lingering enabled for the openclaw user: `sudo loginctl enable-linger openclaw`. Check before install; if not set, `ExecStart` only runs while the user is actively logged in. Run on VPS: `loginctl show-user openclaw | grep Linger`.
- `EnvironmentFile` parses `VAR=VALUE` lines; comments and empty lines ignored, but nothing with shell interpolation (`$VAR` references). `.env` should be clean of those.
- `StandardOutput=append:<path>` requires systemd ≥ 240. VPS is modern Ubuntu so this is fine.

---

## Operational gotchas the previous session hit

These are not in any doc. Future Claude will repeat them otherwise.

1. **SSH self-pkill footgun.** `ssh openclaw@... "pkill -f telegram_inbox.py"` KILLS THE SSH SESSION because the shell command line itself contains the literal string `telegram_inbox.py`, so pkill matches the parent bash. Use two-step instead:
   ```
   ssh openclaw@... "pgrep -f 'python3 agents/shared/telegram_inbox.py' | head -1 | xargs -r kill"
   ```
   Or do it inside a script that's not passed as an ssh argument.

2. **Git auth on VPS.** The VPS has a credential helper that reads `GH_TOKEN` from `~/clawford/.env` (NOT `~/openclaw/.env` — that was the pre-liberation path). If `git pull` on the VPS returns `Authentication failed`, check/fix with:
   ```
   cd ~/repo && git config credential.helper '!f() { echo username=x-access-token; echo password=$(grep GH_TOKEN ~/clawford/.env | cut -d= -f2); }; f'
   ```

3. **Deploy path is always VPS-side.** Do not run `python3 agents/shared/deploy.py <agent>` locally — `deploy.py` writes to `$HOME/.clawford/<agent>-workspace/` on whatever machine it runs on, which is a dead mirror on Windows. Canonical flow: `git commit → push → ssh vps "cd ~/repo && git pull" → ssh vps "python3 agents/shared/deploy.py <agent> --accept-drift --yes-updates"`.

4. **`SHARED_RUNTIME_MODULES` allowlist.** `agents/shared/deploy.py` has an explicit tuple of shared module filenames that get synced to each workspace. Adding a new `agents/shared/*.py` module silently fails to deploy unless you also add it to this list. Line ~1039. Learned the hard way with `subprocess_helpers.py`.

5. **Pre-existing drift on deploy.** `deploy.py` refuses to run if workspace files exist that aren't in the manifest (e.g. liberation-era leftover scripts). Pass `--accept-drift`. Harmless.

6. **Pytest name collision on `test_heartbeat.py`.** Pytest can't collect all heartbeat tests in one run because four agents have a file of that name. Run them per-directory:
   ```
   for d in agents/shared/tests agents/family-calendar agents/meetings-coach agents/connector agents/news-digest agents/shopping/scripts/test_heartbeat.py; do
     python -m pytest "$d"
   done
   ```

7. **Codex Responses backend quirks.**
   - `store: false` is MANDATORY. The endpoint returns 400 `"Store must be set to false"` on `store: true`. No `previous_response_id` chaining — every call is fully stateless.
   - Tool-use IS supported via `tools: [...]` field — confirmed empirically against `chatgpt.com/backend-api/codex/responses`.
   - Function-call arguments must be sent back as a JSON *string* in the echoed `function_call` input item, not as a dict. The helper in `tool_use.py` already does this correctly.

8. **Calendar agents and timezone.** The VPS runs UTC. the operator lives in PT. Any date arithmetic that uses `date.today()` or `datetime.now()` without an explicit tzinfo will be wrong about "today" for ~8h of the day. The calendar tools read `timezone` from agent config; the dispatcher injects the current user-local time into every system prompt. If you're adding date-aware code, do the same.

9. **Google OAuth re-auth is a manual step.** When tokens get revoked (roughly every 6 months for unused apps), run `python agents/family-calendar/scripts/gcal-auth.py --credentials /tmp/creds.json --token /tmp/new-token.json` LOCALLY on the operator's Windows machine (NOT on the VPS via tunnel — the SSH tunnel version has a state-mismatch bug with any cached browser tabs). Then SCP the token up to BOTH `~/.clawford/family-calendar-workspace/token.json` and `~/.clawford/meetings-coach-workspace/token.json` — they share the same OAuth client.

10. **Windows line endings.** `git commit` warns `LF will be replaced by CRLF` on every commit. Harmless but noisy. Don't try to "fix" it.

11. **Memory files auto-load.** `C:\Users\operator\.claude\projects\E--Dropbox-Startup-Clawford\memory\MEMORY.md` is always in context. It has 70+ entries; the most load-bearing for this work are:
    - `feedback_no_api_keys_ever.md` — never hardcode API keys; use the codex subscription path
    - `feedback_deploy_path_is_ssh_pull.md` — ssh→git pull→deploy.py, never local
    - `feedback_no_on_vps_dev.md` — all code changes go through local git
    - `feedback_tdd_mandatory_for_infra.md` — red/green TDD for every infra change
    - `feedback_dont_suggest_stopping.md` — user decides when to end
    - `feedback_execute_dont_instruct.md` — run the commands yourself, don't narrate

---

## How to smoke test after restart

```bash
# 1. Check daemon status
ssh openclaw@203.0.113.10 "ps -ef | grep telegram_inbox | grep -v grep"

# 2. If dead, start it (Phase D will replace with systemctl start clawford-inbox)
ssh openclaw@203.0.113.10 "nohup bash -c 'set -a; source ~/clawford/.env; set +a; cd ~/repo && exec python3 agents/shared/telegram_inbox.py' > ~/.clawford/logs/inbox.log 2>&1 & disown"

# 3. Verify all 6 poll loops started
ssh openclaw@203.0.113.10 "tail -10 ~/.clawford/logs/inbox.log"

# 4. Direct dispatch probe (bypasses Telegram, tests full pipeline)
ssh openclaw@203.0.113.10 "cd ~/repo && set -a && source ~/clawford/.env && set +a && python3 -c '
import sys
sys.path.insert(0, \"agents/shared\")
import dispatcher
u = {\"update_id\": 999, \"message\": {\"from\": {\"id\": 111111111}, \"chat\": {\"id\": 111111111, \"type\": \"private\"}, \"text\": \"hows the fleet?\"}}
dispatcher.dispatch(\"fix-it\", u)
print(\"done\")
'"

# 5. Check the reply landed in conversation log
ssh openclaw@203.0.113.10 "tail -1 ~/.clawford/inbox/fix-it.jsonl | python3 -m json.tool"

# 6. Real Telegram test — message @openclaw_fixit_bot from your phone with "how's the fleet?"
#    Expect: typing indicator, then real reply citing get_fleet_health output
```

If any step fails, read `~/.clawford/logs/inbox.log` for the daemon's exception trace.

---

## Files to read first, in order

1. **This file** (you're reading it)
2. `CHANGELOG.md` — last 10 entries
3. `agents/shared/dispatcher.py` — the core request handler, 250 lines
4. `agents/shared/tool_use.py` — the two-turn loop, 150 lines
5. `agents/shared/telegram_inbox.py` — the async daemon, 280 lines
6. `agents/shared/conversation.py` — window persistence, 90 lines
7. `agents/shared/subprocess_helpers.py` — shared subprocess helper, 115 lines
8. `agents/fix-it/tools.py` — reference tool manifest, 120 lines
9. `agents/shopping/scripts/costco-reorder.py` — existing Hilda reorder entry point (for Phase C design)
10. `agents/family-calendar/scripts/gcal-write.py` — existing calendar write entry point (for Phase C Mouse tools)

---

## Pending action from the human

the operator spot-checked all six bots after Phase B + boundary fix and confirmed they work correctly. He is ready for Phase C.

His stated preference order was: Phase C → Phase D. No plan-mode preamble needed — approved scope, just execute.

**Start with Hilda producer tools** (highest-value agent, clearest validation path — end-to-end reorder from chat).

Good luck.
