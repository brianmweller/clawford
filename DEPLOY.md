# DEPLOY.md — Deploying an OpenClaw Agent

Canonical workflow for deploying or updating an agent on the VPS. This
file supersedes the pre-2026-04-12 shell-script flow; all six
agents (shopping, family-calendar, meetings-coach, news-digest,
connector, fix-it) are now deployed via `agents/shared/deploy.py`
driven by a per-agent `manifest.json`, guarded by eight test-covered
safeguards.

---

## The canonical workflow

```bash
# 1. Edit locally in the Dropbox Clawford repo
#    (E:/Dropbox/Startup/Clawford on Windows)
vim agents/shopping/scripts/costco-orders.py

# 2. Commit — required. deploy.py refuses dirty sources.
git add agents/shopping/scripts/costco-orders.py
git commit -m "shopping: costco-orders timeout bump"

# 3. Push to GitHub
git push origin master

# 4. Pull on the VPS
ssh openclaw@198.51.100.42 "cd ~/repo && git pull"

# 5. Run the hardened deploy tool
ssh openclaw@198.51.100.42 \
  "cd ~/repo && python3 agents/shared/deploy.py shopping"
```

**Do not** `scp` local files directly into `~/repo/` on the VPS. The
2026-04-12 deploy regression happened because a Claude session SCP'd
uncommitted local files into VPS `~/repo/` and ran `deploy.py` over
them, propagating stale content to production workspaces with no audit
trail. The source-cleanliness gate (Safeguard 2) now refuses that
pattern, but the norm is: every change flows through `git commit →
push → pull`.

---

## What deploy.py does and does not do

**Does:**
- Copies manifest-listed config files and scripts from
  `~/repo/agents/<agent>/` → `~/.openclaw/<agent>-workspace/`, honoring
  the chattr-immutable flag on SOUL.md / IDENTITY.md.
- Seeds manifest-listed state files (`grocery-list.json`, etc.) only
  if absent — preserves live accumulated data across reruns.
- Syncs cron definitions via `openclaw cron edit --message` for
  UPDATEs and `cron add` for new. Never creates duplicates.
- Ensures the Telegram channel account + agent binding are registered
  (idempotent).
- Adds each manifest-listed exec allowlist pattern.
- Writes pre-deploy backup tarballs.

**Does not:**
- Read VPS workspace state back into the source repo. Flow is
  strictly `local git → VPS workspace`, one direction. **Any edit
  made directly on the VPS workspace is overwritten on the next
  deploy unless committed back to local git first.**
- Overwrite config files that have drifted from the last deploy's
  recorded state (Safeguard 4 refuses unless `--accept-drift`).
- Apply UPDATEs silently — every UPDATE shows a unified diff and
  requires y/N confirmation unless `--yes-updates` is passed.
- Delete orphan crons (live but not in manifest) unless
  `--remove-orphans`. Warns by default.
- Touch the shared brain at `~/Dropbox/openclaw-backup/`. The brain
  has two layers: **declarative config/code** at `ops/brain/*` in
  git (flow: git → VPS, pushed manually via
  `ops/scripts/sync-brain-to-vps.sh`) and **runtime state** in
  Dropbox-only paths (status files, people/, facts/, commitments/,
  tasks/, queues). State is agent-owned; never rescue it into git.
  See `memory/feedback_brain_rescue_boundary.md`.

---

## Nine safeguards

| # | Name | Flag to override | What it prevents |
|---|---|---|---|
| 1 | Pre-deploy backup | (none — mandatory) | Unrecoverable rollbacks — tar written to `~/.openclaw/deploy-backups/` + mirrored to `~/Dropbox/openclaw-backup/deploy-backups/` |
| 2 | Source-clean gate | `--allow-dirty` | SCP'ing uncommitted local files into `~/repo/` and deploying them |
| 3 | UPDATE diff + confirm | `--yes-updates` | Silent overwrite of a file that shouldn't change |
| 4 | Drift detection (blocking) | `--accept-drift` | Deploys wiping VPS-side edits without audit |
| 5 | Deploy banner | (none — cosmetic) | Ambiguity about source, target, git HEAD, flow direction |
| 6 | Smoke-test hook | `--smoke-test` activates it | Silent regressions — restores backup on cron failure |
| 7 | `openclaw.json` schema validate | (none — mandatory) | Deploying against a gateway whose config is invalid — e.g. a version downgrade that rejects the `streaming: {mode: ...}` object shape, putting the gateway in a restart loop |
| 8 | `exec-approvals` baseline drift guard | (none — mandatory) | `defaults.security=allowlist` or per-agent `policy=allowlist` quietly blocking every cron session after an openclaw upgrade starts enforcing the stricter side — see Gotcha §7 below |
| 9 | Cron message hygiene | (none — mandatory) | Manifest cron messages containing shell-operator bug-attractors (`; echo $?`, `sh -lc python`, `> /tmp/`, `2>&1`, `$(python`) that an LLM would copy verbatim into its exec tool call and hit the openclaw preflight — see Gotcha §10 below |

All nine are test-covered under `agents/shared/tests/` (200+ passing
offline, no VPS required). On drift Safeguard 8 refuses with exit
code 7 and a per-key error list pointing at the drift — fix
`~/.openclaw/exec-approvals.json` first, or update the baseline at
`ops/exec-approvals-baseline.json` if the invariant itself is changing
(e.g., new agent). Safeguard 9 refuses with exit code 8 and the
offending cron name + matched pattern.

---

## Recovery from a bad deploy

```bash
# Find the most recent backup
ls -t ~/.openclaw/deploy-backups/<agent>-*.tar.gz | head -5

# Or from local (if VPS is gone)
ls -t ~/Dropbox/openclaw-backup/deploy-backups/<agent>-*.tar.gz | head -5

# Restore
BACKUP=$(ls -t ~/.openclaw/deploy-backups/<agent>-*.tar.gz | head -1)
rm -rf ~/.openclaw/<agent>-workspace/*
tar -xzf $BACKUP -C ~/.openclaw/ --strip-components=0
```

If Safeguard 6 (`--smoke-test`) was active, the restore is automatic
on cron failure — no human intervention needed.

---

## Manifest schema

Each agent has `agents/<agent_id>/manifest.json`. Minimal example:

```json
{
  "agent_id": "shopping",
  "display_name": "Hilda Hippo",
  "workspace": "~/.openclaw/shopping-workspace",
  "status_file": "~/Dropbox/openclaw-backup/agents/shopping.status.md",
  "telegram": {
    "account": "shopping",
    "bot_token_env": "SHOPPING_BOT_TOKEN"
  },
  "config_files": [
    {"src": "SOUL.md", "immutable": true},
    {"src": "IDENTITY.md", "immutable": true},
    {"src": "TOOLS.md"},
    {"src": "AGENTS.md"},
    {"src": "USER.md"},
    {"src": "HEARTBEAT.md"},
    {"src": "MEMORY.md"},
    {"src": "CRONS.md"}
  ],
  "scripts": ["scripts/amazon-orders.py", "scripts/costco-orders.py"],
  "state_files": [
    {
      "path": "grocery-list.json",
      "seed_if_absent": {"updated_at": null, "items": []}
    }
  ],
  "approvals": {
    "allowlist": ["/usr/bin/*", "/bin/*", "/usr/local/bin/*"]
  },
  "crons": [
    {
      "name": "delivery-digest",
      "cron": "0 14 * * *",
      "announce": true,
      "no_deliver": false,
      "message": "Generate the daily delivery report. ..."
    }
  ],
  "smoke_test": {
    "cron_name": "heartbeat",
    "max_wait_s": 120
  }
}
```

Bootstrap an existing agent's manifest from its legacy `deploy.sh`:

```bash
python3 agents/shared/import_from_deploy_sh.py shopping
```

---

## First-deploy checklist for a new agent

For a brand-new agent that has never been onboarded. **Read the "Gotchas"
section below first** — several of these steps are traps that bit us
during the Huckle Cat (2026-04-12) build.

1. Create the Telegram bot via @BotFather. Save the token as
   `<AGENTNAME>_BOT_TOKEN=<token>` in `/home/openclaw/openclaw/.env` on
   the VPS (not any `/root/openclaw/.env` — that path doesn't exist).
2. Write SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md,
   MEMORY.md, CRONS.md in `agents/<new-agent>/`.
3. Write the agent's scripts under `agents/<new-agent>/scripts/`.
4. Write a `manifest.json`. **Include `"approvals": {"policy": "full",
   "security": "full", "allowlist": [...]}`** — without these, new agents
   default to `policy=null` which falls back to strict allowlist checks
   that don't honor wildcards, triggering approval prompts on every
   shell command the agent tries to run.
5. Commit everything to local git and push.
6. On the VPS: register the agent. The interactive wizard
   (`oci agents add <new-agent>`) is **broken as of 2026-04-12** — it
   tries to OAuth the codex provider with scope `model.request` that
   current OAuth clients can't grant (`invalid_scope` error). Workaround
   is the "bootstrap by direct config" path in Gotchas §1 below.
7. `/start` the bot on Telegram. In openclaw 2026.4.10 the first message
   auto-pairs if the Telegram account + binding are already in
   `openclaw.json` — no explicit `oc pairing approve` needed.
8. `cd ~/repo && git pull && bash agents/<new-agent>/deploy.sh`
   (or `python3 agents/shared/deploy.py <new-agent>`).
9. **Verify SOUL.md is actually loaded.** If the agent still responds
   with a "fresh workspace, who am I?" onboarding dialog, BOOTSTRAP.md
   was auto-created by openclaw's `agents add` wizard and is shadowing
   your IDENTITY.md. `deploy.py` removes BOOTSTRAP.md automatically as
   of 2026-04-12 — but if you see the symptom, verify the file is gone
   at `~/.openclaw/<agent>-workspace/BOOTSTRAP.md`.
10. **Set the bot's slash-command menu. Two steps — both required.**

    **Step 10a — openclaw config.** Edit
    `~/.openclaw/openclaw.json` in the gateway container. For the
    Telegram account belonging to the new agent, set:

    ```json
    "channels": {
      "telegram": {
        "accounts": {
          "<account>": {
            "botToken": "...",
            "enabled": true,
            "name": "<Display Name>",
            "commands": {"native": false},
            "customCommands": [
              {"command": "radar", "description": "Today's nudges"},
              {"command": "find",  "description": "Look up: /find Drew"}
            ]
          }
        }
      }
    }
    ```

    Then restart the gateway so openclaw re-runs its command sync. If
    you skip this step, on the next gateway restart openclaw will
    clobber any commands you set directly via the Bot API — it
    maintains its own hash-cached state in
    `~/.openclaw/telegram/command-hash-<account>-*.txt` and resyncs on
    startup whenever the hash differs. **Why `commands.native: false`:
    without it, openclaw injects all ~49 built-in commands
    (`/help`, `/status`, `/context`, `/tools`, `/exec`, …) into the
    bot's menu alongside your 7 custom ones, producing a 56-item menu
    that makes the agent look generic.**

    **Step 10b — the Telegram Bot API set.** Even with openclaw
    managing its own command sync, you may want to set commands
    directly via the Bot API for immediate effect (openclaw's sync
    runs on startup and may be skipped if the hash matches). Both
    `default` and `all_private_chats` scopes should be set — the
    Telegram client picks the most specific scope when showing the
    menu, so `all_private_chats` wins in DMs:

    ```python
    import json, urllib.request
    TOKEN = "<YOUR_BOT_TOKEN>"
    def call(method, body=None):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode())

    cmds = [
        {"command": "radar",     "description": "Today's nudges"},
        {"command": "find",      "description": "Look up: /find Drew"},
        ...
    ]
    call("setMyCommands", {"commands": cmds})
    call("setMyCommands", {"commands": cmds, "scope": {"type": "all_private_chats"}})
    call("setMyDescription", {"description": "..."})
    call("setMyShortDescription", {"short_description": "..."})
    ```

    **In practice, do not write the `python -c` by hand.** Two
    canonical scripts live in `ops/scripts/` and run automatically
    on every container start via `entrypoint.sh` hooks:

    - `ops/scripts/set-bot-commands.sh` — sets `setMyCommands` for
      all 6 agent bots. Runs ~25s after gateway start.
    - `ops/scripts/set-bot-descriptions.sh` — sets `setMyDescription`
      + `setMyShortDescription` for all 6 agent bots. Runs ~30s
      after gateway start. Without this the empty-chat window is
      blank — no hint of what the agent does. Voice matches Huckle
      Cat's "Part of the Busytown OpenClaw network" tagline.

    Both scripts are idempotent. Edit the script in git, push, pull,
    and either `docker compose restart` (auto-runs both via the
    entrypoint hooks) OR run them once by hand:

    ```
    ssh openclaw@198.51.100.42 "bash ~/repo/ops/scripts/set-bot-commands.sh"
    ssh openclaw@198.51.100.42 "bash ~/repo/ops/scripts/set-bot-descriptions.sh"
    ```

    **Gotchas:**
    - git-bash `curl` on Windows mangles UTF-8 quotes in JSON bodies.
      Use Python's `urllib.request` with explicit UTF-8 encoding —
      both canonical scripts already do this.
    - After setting via Bot API, force-refresh the Telegram client to
      clear its cache: pull-down on the chat, or close/reopen the app.
      Telegram caches command menus AND descriptions aggressively on
      the client side; you may need to close+reopen the chat entirely
      to see new descriptions in the empty-chat window.
    - If commands were previously wrong, wipe the openclaw hash cache
      before restarting:
      `rm /home/node/.openclaw/telegram/command-hash-<account>-*.txt`
    - openclaw NEVER touches the description fields (only commands),
      so set-bot-descriptions.sh has no clobber risk — it's in the
      entrypoint hook only to survive `docker compose up --build`.

11. Smoke-test via Telegram: send a message, confirm the agent reads
    SOUL.md and runs its scripts without approval prompts.

For a RE-deploy (updating an existing agent): skip steps 1-3, 6-7, 10.
Just commit, push, pull, deploy. Step 10 only needs to be re-run when
the command list changes.

---

## Gotchas and lessons learned (Huckle Cat, 2026-04-12)

Six unrelated failures stacked during a single agent onboarding and
turned a "push the button" deploy into a two-hour slog. Each one is now
either fixed in deploy.py or documented below.

### 1. `openclaw agents add` wizard OAuth failure

**Symptom:** The wizard opens a browser to Codex OAuth, the callback
URL returns
`error=invalid_scope&error_description=The+requested+scope+is+invalid...+'model.request'`.

**Root cause:** The wizard's codex provider requests a `model.request`
scope that the installed OAuth client is not authorized to grant.
OpenClaw 2026.4.10 ships this broken.

**Workaround — bootstrap by direct config:**
```bash
# 1. Copy an existing agent's auth-profiles.json to the new agent
ssh openclaw@198.51.100.42 "docker exec openclaw-openclaw-gateway-1 sh -c \
  'mkdir -p /home/node/.openclaw/agents/<new-agent>/agent && \
   cp /home/node/.openclaw/agents/main/agent/auth-profiles.json \
      /home/node/.openclaw/agents/<new-agent>/agent/auth-profiles.json && \
   cat > /home/node/.openclaw/agents/<new-agent>/agent/auth-state.json <<EOF
{\"version\":1,\"lastGood\":{\"openai-codex\":\"openai-codex:<your-account>\"},
 \"usageStats\":{\"openai-codex:<your-account>\":{\"errorCount\":0,\"lastUsed\":0}}}
EOF'"

# 2. Append the agent entry to openclaw.json manually
ssh openclaw@198.51.100.42 "docker exec openclaw-openclaw-gateway-1 python3 -c '
import json
p = \"/home/node/.openclaw/openclaw.json\"
d = json.load(open(p))
d[\"agents\"][\"list\"].append({
  \"id\": \"<new-agent>\", \"name\": \"<new-agent>\",
  \"workspace\": \"/home/node/.openclaw/<new-agent>-workspace\",
  \"agentDir\": \"/home/node/.openclaw/agents/<new-agent>/agent\",
  \"model\": \"openai-codex/gpt-5.4\",
  \"identity\": {\"name\": \"<display>\", \"emoji\": \"🐱\", \"theme\": \"...\"}
})
json.dump(d, open(p, \"w\"), indent=2)'"

# 3. Add the Telegram account + binding (also via direct json edit)
# 4. Restart gateway and verify with `oc agents list`
docker restart openclaw-openclaw-gateway-1
```

This path works because codex OAuth is actually a single shared
credential — all agents on the same ChatGPT account use the same
profile. Copying `auth-profiles.json` from a working agent skips the
broken wizard entirely.

### 2. `exec policy=null` silently defaults to strict allowlist

**Symptom:** Agent tries to run `find`, `cat`, `ls` and every command
triggers a `/approve <uuid>` gate on Telegram despite the manifest
having `"allowlist": ["/usr/bin/*", "/bin/*", ...]`.

**Root cause:** In `~/.openclaw/exec-approvals.json`, new agents get
`{"policy": null, "security": null}`. The wildcard allowlist only
applies when `policy=allowlist` is set. Null falls through to a default
that rejects unknown commands.

**Fix (now permanent):** `manifest.json` takes `"approvals": {"policy":
"full", "security": "full"}`. `deploy.py` edits `exec-approvals.json`
on each deploy to enforce these values. As long as your manifest sets
them, you'll never hit this again.

### 3. `BOOTSTRAP.md` shadowing `IDENTITY.md`

**Symptom:** Deployed SOUL.md + IDENTITY.md are correct, but the agent
responds in Telegram with "I just came online. What should I call
you?" — the blank-workspace onboarding dialog.

**Root cause:** `openclaw agents add` (or its failure partway through)
writes a `BOOTSTRAP.md` file into the workspace. That file tells the
agent to run a "who am I?" onboarding conversation. It takes precedence
over SOUL/IDENTITY because it's the "fresh boot" script.

**Fix (now permanent):** `deploy.py` detects BOOTSTRAP.md and removes
it whenever the manifest includes IDENTITY.md.

### 4. `deploy_wrapper.sh` BASH_SOURCE stack was wrong

**Symptom:** `bash agents/<agent>/deploy.sh` fails immediately with
`deploy_wrapper.sh must be invoked from an agent's deploy.sh`, even
though that's exactly how it was invoked.

**Root cause:** The wrapper used `${BASH_SOURCE[1]}` to detect the
caller. But `exec bash wrapper.sh` (the pattern used in every agent's
deploy.sh) replaces the shell process, so BASH_SOURCE is reset —
`[1]` is empty, `[0]` is the wrapper itself, and the basename of
`../shared/` is `shared`, which the guard correctly refuses.

**Fix (now permanent):** Each `deploy.sh` now sets `OPENCLAW_AGENT_ID`
in the environment before `exec`ing the wrapper. The wrapper reads from
the env var. `bash agents/<agent>/deploy.sh` now works.

### 5. Python `~/Dropbox/` resolves to the wrong account on Windows

**Symptom:** Seeded files (people/facts) appear locally but never sync
to the VPS. The VPS people directory stays nearly empty.

**Root cause:** Sam has **two** Dropbox accounts on Windows:
- `C:\Users\Sam\Dropbox` — personal, does NOT sync to the VPS
- `E:\Dropbox\openclaw-backup` — Clawford work Dropbox, syncs to VPS

Python's `os.path.expanduser("~/Dropbox/openclaw-backup/")` resolves
to the personal one. Any script that writes to the shared brain via
that path silently drops files into a void.

**Fix:** Hardcode `E:/Dropbox/openclaw-backup/` on Windows with a Unix
fallback to `~/Dropbox/openclaw-backup/` for the VPS. See
`people-seed-from-mine.py` for the pattern. Memory entry at
`memory/feedback_dropbox_path.md`.

### 6. `people-scan.py` parser expected the wrong markdown style

**Symptom:** `people-scan.py --overdue-only` returns `summary: {total:
0, skipped: 282}` on a directory of correctly-formatted person files.

**Root cause:** The parser regex was
`r"- \*\*(\w[\w_]*)\*\*:\s*(.*)"` which matches `- **key**: value`.
But the person-file template emits `- **key:** value` (colon INSIDE the
bold). Every file silently failed to parse, contributing zero fields,
and every person silently got "skipped" with no warning that the parser
saw the file but couldn't extract anything.

**Fix (now permanent):** The regex now accepts both
`**key:**`-style and `**key**:`-style — see
`agents/connector/scripts/people-scan.py`. Future format drift will
need a better safeguard: parser should warn when a file has
`- **` prefixes that don't match the regex.

### 7. exec-approvals layered security: "stricter side wins" (2026-04-13)

**Symptom:** Every agent across the fleet suddenly shows
`approval required` on every script the crons run. Mr Fixit's
heartbeat, Hilda's costco-token-daemon, Lowly Worm's fetch-and-rank
— all blocked on `/approve <uuid>` prompts, none of them firing
the allowlist wildcards their manifests declared.

**Root cause:** `~/.openclaw/exec-approvals.json` had
`defaults.security = "allowlist"` (not `"full"`), plus
`agents.main.policy = "allowlist"` with only 2 patterns
(`/usr/local/bin/openclaw`, `/usr/bin/sleep`). This had been latent
for weeks. An openclaw upgrade started intersecting defaults with
per-agent policy more strictly ("stricter side wins"), and the
agents that had individually declared `policy=full` suddenly had
their effective policy narrowed back to the intersection with
`main.policy=allowlist`. Every cron hit approval prompts that
no human was there to resolve.

**Fix (now permanent):**
1. `~/.openclaw/exec-approvals.json` patched in place:
   `defaults.security="full"`, every agent `security=full policy=full
   ask=off` (backup at `.bak-pre-full`).
2. Committed an invariant schema to git at
   `ops/exec-approvals-baseline.json` — NOT the raw file (too
   volatile: `lastUsedAt`, `lastUsedCommand`, `lastResolvedPath`
   update every exec), just the 2 `defaults` fields and 3
   `agents.<id>` fields that actually define the policy.
3. Wired **Safeguard 8** in `deploy.py` to compare the live
   `openclaw approvals get --json` output to the baseline on every
   deploy and refuse with exit 7 on drift. Extra allowlist entries
   in the live file are tolerated; only the baseline keys are enforced.

**Lesson:** Do not commit volatile config files raw. Commit the
invariant schema — the things that must be true for the system to
work — and write a test-covered guard that checks live state
against it. "Raw file in git drifts every minute" is a sign that
you need a schema commitment, not a raw commitment.

### 8. `openclaw.json` account bindings that reference non-existent accounts (2026-04-13)

**Symptom:** Half the bots "forgot their commands." Mr Fixit's
slash menu shrinks from 3 custom commands to 1. Sergeant Murphy's
menu disappears entirely. No git change triggered it. Nothing in
the logs mentions the Telegram bot.

**Root cause:** `~/.openclaw/openclaw.json` had a stale
`bindings[]` entry whose `match.accountId = "fixit"` pointed at
an account that no longer existed (it had been renamed to
`default` months earlier). When openclaw's startup command-sync
pass walks bindings, it treats an unmatched binding as "agent has
no custom commands" and pushes an empty command list to the bot,
clobbering whatever was set via the Bot API in Step 10b.

**Fix (now permanent):**
1. `~/.openclaw/openclaw.json`: `bindings[6].match.accountId` fixed
   from `"fixit"` to `"default"`; backup at `.bak-pre-route-fix`.
2. Added `ops/scripts/set-bot-commands.sh` (belt-and-suspenders
   Step 10b) to reapply the full command set via the Bot API for
   all 5 accounts. Idempotent, safe to re-run.
3. Every account in openclaw.json now has `commands.native: false`
   plus its `customCommands` array spelled out, so openclaw's
   sync pass is a no-op rather than a clobber.

**Lesson:** Stale config pointing at removed objects is a latent
landmine. Either openclaw should WARN on unmatched bindings at
startup (it doesn't), or deploy-time validation should walk
bindings and verify every `accountId`/`agentId` they reference
still exists. For now: if a bot's menu silently regresses, suspect
openclaw's command sync, not the Bot API.

### 9. `docker compose up --build` can downgrade openclaw (2026-04-12)

**Symptom:** `deploy.py` refuses with
`channels.telegram.streaming: Invalid input` across every agent.
The streaming config was unchanged in git. The gateway is in a
restart loop.

**Root cause:** `docker compose -f ~/openclaw/docker-compose.yml up
-d --build` rebuilds the image **without refreshing the
`OPENCLAW_VERSION` build arg** — if the Dockerfile's default
`OPENCLAW_VERSION` is behind the latest published version, the
build downgrades the CLI inside the image. The older CLI
doesn't understand the newer config's `streaming: {mode: "off"}`
object shape (it expected a bare string), so schema validation
fails, gateway loops, every deploy halts.

**Fix (now permanent):**
1. `ops/Dockerfile` `ARG OPENCLAW_VERSION=2026.4.11` kept in sync
   with the currently-targeted release.
2. **Safeguard 7** added to `deploy.py` — shells to
   `openclaw config validate --json` before any file writes and
   refuses on schema errors. Would have caught this class at the
   very first agent re-deploy instead of silently proceeding.

**Lesson:** Build args don't autobump. Either pin the version in
the Dockerfile and treat it as a tracked dependency (what we do),
or pass `--build-arg OPENCLAW_VERSION=...` on every compose up.
And add a config-validate gate *before* file writes so an invalid
config never costs you a recovery round.

### 10. Telegram clickable commands strip their args on tap (2026-04-13)

**Symptom:** Digest messages show `/like 1`, `/dislike 2`, `/more 3`
per item. Sam taps a link. Worm replies: "I got the like, but I
need the item number. Send /like 1 or just like:1 and I'll note it."

**Root cause:** Telegram's auto-detected slash commands render the
`/verb` as a clickable hyperlink AND parse any following tokens as
arguments. When the user TAPS the link, only the command part
(`/like`) is sent — the space-separated arg (`1`) is stripped.
The typed form (user manually types `/like 1` into the input) does
send the full message, but the tap form does not. There's no way
to make auto-detected commands include args on tap.

**Fix (now the default for news-digest):** Use inline keyboard
buttons with `callback_data`:

```python
buttons = {
    "inline_keyboard": [[
        {"text": "👍 like",    "callback_data": f"like:{num}"},
        {"text": "👎 dislike", "callback_data": f"dislike:{num}"},
        {"text": "📖 more",    "callback_data": f"more:{num}"},
    ]]
}
send_telegram(token, chat, text, reply_markup=buttons)
```

Tap = instant, arbitrary display text, `callback_data` is delivered
verbatim. Openclaw forwards the callback_query into the agent's
session transcript as a text message containing the callback_data
value, which `engagement-poller.py` matches via
`re.search(r"(?<![a-z])/?(like|dislike|more)[:_\s]\s*(\d+)", ...)`.

See `agents/fix-it/scripts/morning-fleet-deliver.py::_buttons_for_item`
and the pattern revived from commit `52bc358`.

**Fallback for plain text** (when `reply_markup` isn't available):
the underscore form `/like_1 /dislike_1 /more_1` sends the full token
on tap because underscore is part of the command name per the Bot
API spec.

**Pre-existing bug caught while fixing this:** the old
`extract_engagement` regex `(?:/like|like[:\s])\s*(\d+)` matched
"like" as a substring of "dislike", so every `/dislike N` was
silently misclassified as `thumbs_up`. New regex uses a word-boundary
lookbehind `(?<![a-z])` and scans for the leftmost match across all
three verbs. Memory: `feedback_telegram_clickable_commands.md`.

### 11. OpenClaw LLM cron 600s hard timeout (2026-04-13)

**Symptom:** `morning-edition` cron fires, runs for exactly 10
minutes, then finishes with
`{"status": "error", "error": "cron: job execution timed out"}`.
Neither the `morning-items.json` nor the `morning-brief-ready.txt`
output file exists. Next run's scheduled time is written but the
current session produced nothing.

**Root cause:** openclaw caps LLM cron executions at **600 seconds**
(10 min) hard. No checkpointing, no partial output. When the budget
runs out, the session is killed and everything it was doing is lost.

Observed trigger: the new Option B `morning-edition` prompt asks the
LLM to produce TWO files (a structured JSON array plus a plain text
brief) and to read several intermediate cache files. One of those
files (`cache/linkedin-2026-04-13.json`) was missing — the LLM hit
ENOENT, retried, retried, and burned through the 10-minute budget
before making progress.

**Fix (design principle):** For deterministic structured-output
work, bypass the LLM cron entirely and call `openclaw infer model
run --prompt ... --json` from a plain Python wrapper. Each `infer`
call is a one-shot outside the agent cron scheduler, so the 600s
budget doesn't apply. Example pattern:

- `fetch-and-rank.py` writes `cache/ranked-<date>.json` (structured
  data)
- A new Python composer reads the ranked file, calls `openclaw
  infer` once per item to generate the extended headline, writes
  `cache/morning-items.json`
- `morning-fleet-deliver.py` reads `morning-items.json` and sends
  per-item Telegram messages with inline keyboards

The LLM is still doing the creative work (1-sentence editorial
headlines), but it's being driven by a Python script that can't
time out the same way a cron session does. This pattern is live
in `agents/news-digest/scripts/update-preferences.py::call_judge_llm`
(commit `844a251`) for the nightly judge LLM that extracts
fine-grained subtopic tags from engagement events.

**Emergency bypass** (when a cron is stuck and you need to unblock
delivery RIGHT NOW):

```python
ssh openclaw@vps "python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path('cache/ranked-2026-04-13.json').read_text())
items = [
    {'num': i+1, 'category': a.get('category'), 'title': a.get('title'),
     'extended_headline': a.get('summary', '')[:250],
     'url': a.get('link'), 'source_label': a.get('source_label')}
    for i, a in enumerate(data['articles'][:15])
]
Path('cache/morning-items.json').write_text(json.dumps(items, indent=2))
PY"
```

Then run the delivery script directly. You skip the LLM cron
entirely for that run. Used 2026-04-13 to unblock Option B
verification. Memory: `feedback_openclaw_cron_timeout.md`.

### 12. Playwright `.click()` default 30s retry blows scraper budgets (2026-04-13)

**Symptom:** `linkedin-scrape.py` repeatedly returns
`messages: 0` despite the recency filter accepting 15 threads.
Running the scraper directly shows Playwright logs spinning on
"element is visible, enabled and stable … element is outside of
the viewport … retrying click action" for minutes at a time, and
the overall scraper hits its 180-second timeout before any thread's
content is read.

**Root cause:** Playwright's default `click()` waits up to **30
seconds** for the element to be visible-AND-stable-AND-inside-the-
viewport before giving up. LinkedIn's inbox puts message list items
outside the visible viewport when the list is long, so Playwright
enters the "visible but outside viewport" retry loop. One stuck
thread eats 30s, and 15 × 30s = 450s is way more than any reasonable
scraper budget.

**Fix (now permanent):** explicit 3-step bounded click:

```python
try:
    thread_el.scroll_into_view_if_needed(timeout=3000)
except Exception:
    pass
try:
    thread_el.click(timeout=5000)
    clicked = True
except Exception:
    try:
        thread_el.click(force=True, timeout=3000)
        clicked = True
    except Exception:
        clicked = False
if not clicked:
    enriched.append(thread)  # keep the preview, skip click-through
    continue
```

See `agents/news-digest/scripts/linkedin-scrape.py::scrape_messages`
(commit `eba5fa4`). Worst-case ~11s per thread, 15 × 11s = 165s,
fits the 180s budget. Failed clicks fall through to "preview-only"
mode rather than blocking the whole scraper.

**Lesson:** Any Playwright `.click()` inside a bounded-budget
scraper needs an explicit `timeout=` and a fallback path. The
30-second default is fine for interactive debugging and wrong for
cron-invoked scrapers.

### 13. LinkedIn seen-set dedup returns 0 — not broken, just quiet (2026-04-13)

**Symptom:** LinkedIn scraper finishes cleanly, extracts 15 threads,
but `linkedin-<date>.json` contains `messages: 0`. The digest has
no LinkedIn content. First instinct: the scraper is broken.

**Actual behavior:** `linkedin-seen.json` tracks a set of content
hashes for every thread/post/notification ever surfaced. On each
run, the scraper hashes each newly-extracted thread and drops any
hash that's already in the seen-set. Result: if the user hasn't
received a new reply in any thread since the previous successful
scrape, all 15 threads hash the same way and `messages_skipped == 15`
and `messages == 0`.

**Verification (safe and reversible):**

```bash
# Back up the current seen-set
mv ~/.openclaw/news-digest-workspace/cache/linkedin-seen.json{,.bak-test}

# Delete today's cache to force a fresh scrape
rm ~/.openclaw/news-digest-workspace/cache/linkedin-2026-04-13.json

# Run the scraper
docker exec openclaw-gateway python3 \
  /home/node/.openclaw/news-digest-workspace/scripts/linkedin-scrape.py

# If messages > 0 appears, dedup WAS over-aggressive (unlikely but possible)
# If messages == 0 still, the user genuinely has no new content
# — 0 is the correct, working answer.

# Restore the backup so tomorrow's scrape doesn't re-surface 15 stale threads
mv ~/.openclaw/news-digest-workspace/cache/linkedin-seen.json{.bak-test,}
rm ~/.openclaw/news-digest-workspace/cache/linkedin-2026-04-13.json
```

Ask the user "did you get any new LinkedIn replies today?" before
treating `messages: 0` as a bug. 2026-04-13: Sam confirmed "no
new messages today" and the pipeline was working correctly end-to-end.

### 14. Morning delivery runs off the LLM cron scheduler via host cron (2026-04-13)

**Context:** openclaw's LLM cron scheduler runs agent sessions
**serially per agent**. The morning compose burst (5 agents × a
5-7 minute gather LLM session each) hit a queue window where
`morning-fleet-deliver`'s 11:55 UTC LLM session couldn't dispatch
until the `connector/morning-relationship-nudge` finished at ~12:04
UTC. The hold-until-12:00 barrier in the script had already passed
and delivery went out at 5:05 AM PDT instead of 5:00.

**Fix (commit `4e97b90`):**

1. **Shift 5 compose crons 11:30 → 10:30 UTC**. Gives the composers
   90 minutes of slack before the 12:00 deliver target (was 30 min)
   and moves the morning burst out of the 11:30-12:00 window that
   was blocking `*/5` reminder-check, `*/5` engagement-poll,
   heartbeats, and the fleet deliver.

2. **Move `morning-fleet-deliver` OFF the LLM cron entirely** onto
   a plain host crontab entry at `0 12 * * *`. The wrapper at
   `ops/scripts/morning-fleet-deliver-host.sh` does a `docker exec`
   into the gateway container and invokes the existing
   `morning-fleet-deliver.py` script unchanged. The script's
   `wait_until_target_utc_hour(12)` barrier becomes a no-op (host
   cron fires exactly at 12:00) but remains as a defensive layer.

**Architecture boundary:** openclaw LLM cron is for work that
genuinely needs the agent's SOUL/TOOLS context (conversational
replies, multi-step tool use, personality-bearing responses). Host
cron is for deterministic scripted work (delivery, deduplication,
snapshotting, health probes). Script-contract compliance makes the
host-cron migration trivial: any script that prints compliant JSON
and always exits 0 can be invoked via
`ops/scripts/script-contract-host.sh` with a per-agent bot token
and Telegram will get the `alert` field on non-ok status. See
`ops/scripts/script-contract-host.sh` (commit `6185415`) for the
pattern; `shopping/heartbeat`, `meetings-coach/heartbeat`,
`fix-it/heartbeat-check`, and `news-digest/linkedin-keepalive` are
already migrated.

---

## Meta-lesson: why deploy was hard

Every one of the above was a **silent** failure. None of them produced
a clear error that pointed at the fix. The wizard crashed with a cryptic
OAuth scope error. The bot spoke but in the wrong persona. The script
returned empty results. The deploy refused without saying which file
drifted. Each symptom looked unrelated and required a deep dive.

The cumulative fix is: **make every silent failure loud**. `deploy.py`
now emits explicit log lines for policy updates, file removes, and
drift detection. Parser and loader code needs to warn when it sees
data but can't extract anything. The OAuth wizard needs a fallback.
And first-deploy checklists need to anticipate that the happy path
won't work.

Treat a first-agent-onboard like a dependency bring-up: expect five
things to go wrong, verify each step before moving on.

---

## OpenClaw CLI quick reference

All commands prefixed with `oc` (Docker exec wrapper — define in your shell):

```bash
oc() { docker compose -f ~/openclaw/docker-compose.yml exec -T openclaw-gateway openclaw "$@"; }
oci() { docker compose -f ~/openclaw/docker-compose.yml exec -it openclaw-gateway openclaw "$@"; }
```

| Action | Command |
|--------|---------|
| Health | `oc health` |
| List agents | `oc agents list` |
| Add agent (interactive) | `oci agents add {name}` |
| List crons | `oc cron list` |
| Trigger cron | `oc cron run {cron-id}` |
| View cron history | `oc cron runs --id {cron-id}` |
| Edit cron | `oc cron edit {cron-id} --message "..."` |
| Pair Telegram | `oc pairing approve telegram {CODE}` |
| View approvals | `oc approvals get` |

See `guide/09-cli-reference.md` for the full reference.
