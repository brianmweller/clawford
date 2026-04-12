# DEPLOY.md — Deploying an OpenClaw Agent

Canonical workflow for deploying or updating an agent on the VPS. This
file supersedes the pre-2026-04-12 shell-script flow; all five
non-fix-it agents (shopping, family-calendar, meetings-coach,
news-digest, connector) are now deployed via
`agents/shared/deploy.py` driven by a per-agent `manifest.json`.

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

---

## Six safeguards

| # | Name | Flag to override | What it prevents |
|---|---|---|---|
| 1 | Pre-deploy backup | (none — mandatory) | Unrecoverable rollbacks — tar written to `~/.openclaw/deploy-backups/` + mirrored to `~/Dropbox/openclaw-backup/deploy-backups/` |
| 2 | Source-clean gate | `--allow-dirty` | SCP'ing uncommitted local files into `~/repo/` and deploying them |
| 3 | UPDATE diff + confirm | `--yes-updates` | Silent overwrite of a file that shouldn't change |
| 4 | Drift detection (blocking) | `--accept-drift` | Deploys wiping VPS-side edits without audit |
| 5 | Deploy banner | (none — cosmetic) | Ambiguity about source, target, git HEAD, flow direction |
| 6 | Smoke-test hook | `--smoke-test` activates it | Silent regressions — restores backup on cron failure |

All six are test-covered under `agents/shared/tests/` (18/18 passing
offline, no VPS required).

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

    **Gotchas:**
    - git-bash `curl` on Windows mangles UTF-8 quotes in JSON bodies.
      Use Python's `urllib.request` with explicit UTF-8 encoding.
    - After setting via Bot API, force-refresh the Telegram client to
      clear its cache: pull-down on the chat, or close/reopen the app.
      Telegram caches command menus aggressively on the client side.
    - If commands were previously wrong, wipe the openclaw hash cache
      before restarting:
      `rm /home/node/.openclaw/telegram/command-hash-<account>-*.txt`

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
