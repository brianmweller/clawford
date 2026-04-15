# DEPLOY.md — Deploying a Clawford Agent

Canonical workflow for deploying or updating an agent on the VPS.
After the Phase 7 liberation (April 2026), every agent runs as plain
host crons invoking Python directly, with `agents/shared/deploy.py`
as the file-sync + validation tool, driven by a per-agent
`manifest.json` and guarded by nine test-covered safeguards.

> **Historical note.** The "OpenClaw Gotchas" appendix later in this
> document is preserved as an archaeological record of the
> pre-Phase-7 era. Sections numbered 1 through ~14 describe failures
> in the OpenClaw runtime, the gateway container, and the LLM cron
> scheduler — all of which were retired during the liberation. They
> stay here because the scar tissue is useful when something
> resembles an old failure mode, but they are not load-bearing
> documentation.

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
ssh openclaw@198.51.100.42 "cd ~/repo && git pull --ff-only origin master"

# 5. Run the deploy tool ON THE VPS
ssh openclaw@198.51.100.42 \
  "cd ~/repo && python3 agents/shared/deploy.py shopping --yes-updates"
```

**Do not invoke `deploy.py` on your laptop** — it writes to
`$HOME/.clawford/<agent>-workspace/` on whatever box runs it. On the
VPS that's the production workspace; on a laptop it's a dead local
mirror that nothing reads. A laptop dry-run is useful for validating
a manifest change without touching the VPS, and not much else.

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
  `~/repo/agents/<agent>/` → `~/.clawford/<agent>-workspace/`,
  honoring the chattr-immutable flag on SOUL.md / IDENTITY.md.
- Seeds manifest-listed state files (`grocery-list.json`, etc.) only
  if absent — preserves live accumulated data across reruns.
- Mirrors `agents/shared/*.py` runtime modules into the workspace's
  `agents/shared/` so per-agent scripts can import them via the
  standard sys.path shim.
- Writes pre-deploy backup tarballs to
  `~/.clawford/deploy-backups/` and mirrors them to
  `~/Dropbox/openclaw-backup/deploy-backups/` for off-VPS retention.

**Does not:**
- Read VPS workspace state back into the source repo. Flow is
  strictly `local git → VPS workspace`, one direction. **Any edit
  made directly on the VPS workspace is overwritten on the next
  deploy unless committed back to local git first.**
- Overwrite config files that have drifted from the last deploy's
  recorded state (Safeguard 4 refuses unless `--accept-drift`).
- Apply UPDATEs silently — every UPDATE shows a unified diff and
  requires y/N confirmation unless `--yes-updates` is passed.
- Touch the host crontab. Cron registration is owned by
  `ops/scripts/install-host-cron.sh` — re-run it any time the
  contract entry list in that script changes.
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
| 1 | Pre-deploy backup | (none — mandatory) | Unrecoverable rollbacks — tar written to `~/.clawford/deploy-backups/` + mirrored to `~/Dropbox/openclaw-backup/deploy-backups/` |
| 2 | Source-clean gate | `--allow-dirty` | SCP'ing uncommitted local files into `~/repo/` and deploying them |
| 3 | UPDATE diff + confirm | `--yes-updates` | Silent overwrite of a file that shouldn't change |
| 4 | Drift detection (blocking) | `--accept-drift` | Deploys wiping VPS-side edits without audit |
| 5 | Deploy banner | (none — cosmetic) | Ambiguity about source, target, git HEAD, flow direction |
| 6 | Smoke-test hook | `--smoke-test` activates it | Silent regressions — runs `manifest.smoke_test.script` (defaults to `scripts/heartbeat.py`) as a host subprocess, asserts exit 0 + non-empty stdout, auto-restores backup on failure |
| 7 | Manifest validation | (none — mandatory) | Deploying a manifest with duplicate cron names, missing SOUL.md / IDENTITY.md, dangling `smoke_test.script` refs, absolute state-file paths, or `agent_id` / directory mismatch. Pure-Python cross-check; was `openclaw config validate` until Phase 5 liberation. |
| 8 | *(retired 2026-04-15)* | — | Was `exec-approvals` baseline drift. Removed in Phase 5 because the OpenClaw approvals concept no longer exists. Tombstone comment in `deploy.py`; baseline file deleted in Phase 7a. |
| 9 | Cron message hygiene | (none — mandatory) | Manifest cron messages containing shell-operator bug-attractors (`; echo $?`, `sh -lc python`, `> /tmp/`, `2>&1`, `$(python`) that an LLM would copy verbatim into its exec tool call. |
| 10 | Config source resolution | `--skip-files` | Deploying with missing/placeholder-laden real config files — walks `config_files[]`, refuses if any real file is missing or still carries the `CLAWFORD_BOOTSTRAP_UNEDITED` sentinel. `--bootstrap-configs` scaffolds from `.example` siblings. |
| 11 | *(retired 2026-04-15)* | — | Was `docker-compose.yml` drift detection. Removed in Phase 7a because the gateway container is gone and `ops/docker-compose.yml` was deleted from git. |

All nine live safeguards (1–7, 9, 10) are test-covered under
`agents/shared/tests/` (425+ passing offline, no VPS required).
Safeguard 7 refuses with exit code 6 and a per-field list. Safeguard
9 refuses with exit code 8 and the offending cron name + matched
pattern. Structural test `test_phase7_openclaw_helpers_deleted`
enforces the OpenClaw helper deletion at the module level.

---

## Recovery from a bad deploy

```bash
# Find the most recent backup
ls -t ~/.clawford/deploy-backups/<agent>-*.tar.gz | head -5

# Or from local (if VPS is gone)
ls -t ~/Dropbox/openclaw-backup/deploy-backups/<agent>-*.tar.gz | head -5

# Restore
BACKUP=$(ls -t ~/.clawford/deploy-backups/<agent>-*.tar.gz | head -1)
rm -rf ~/.clawford/<agent>-workspace/*
tar -xzf $BACKUP -C ~/.clawford/ --strip-components=0
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
  "workspace": "~/.clawford/shopping-workspace",
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
    "script": "scripts/heartbeat.py",
    "max_wait_s": 120
  }
}
```

> Manifest files may carry a vestigial top-level `"approvals"` block
> (allowlist / policy / security). It's parsed and ignored by
> `deploy.py` post-Phase-7a — the OpenClaw exec-approvals layer that
> consumed it no longer exists. Keeping the block in old manifests is
> harmless; new agents should omit it.

---

## First-deploy checklist for a new agent

For a brand-new agent that has never been onboarded:

1. **Create the Telegram bot** via @BotFather. Save the token as
   `<AGENTNAME>_BOT_TOKEN=<token>` in `~/clawford/.env` on the VPS.
2. **Write workspace files** in `agents/<new-agent>/`: SOUL.md,
   IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md, MEMORY.md,
   CRONS.md. Commit `.example` templates to git; the real PII-hydrated
   files are gitignored and live on your dev box + the VPS only.
3. **Write the agent's scripts** under `agents/<new-agent>/scripts/`,
   each conforming to `agents/shared/SCRIPT_CONTRACT.md` (one JSON
   line on stdout with a `status` field).
4. **Write a `manifest.json`** with `agent_id`, `display_name`,
   `workspace` (`~/.clawford/<agent>-workspace`), `status_file`
   (`~/Dropbox/openclaw-backup/agents/<agent>.status.md`),
   `telegram.account` + `bot_token_env`, `config_files[]`,
   `scripts[]`, `state_files[]`, `crons[]`, and optionally
   `smoke_test`.
5. **Commit everything to local git and push.**
6. **Add the agent to `agents/shared/fleet-manifest.json`** so
   `fleet-health.py` probes its heartbeat.
7. **Add the agent's host crons to `ops/scripts/install-host-cron.sh`**
   (CONTRACT_ENTRIES for script-contract-host.sh-wrapped scripts,
   DIRECT_ENTRIES for dedicated wrappers).
8. **SSH to the VPS and pull:**
   ```bash
   ssh openclaw@198.51.100.42 "cd ~/repo && git pull --ff-only origin master"
   ```
9. **Deploy:**
   ```bash
   ssh openclaw@198.51.100.42 \
     "cd ~/repo && python3 agents/shared/deploy.py <new-agent> --yes-updates"
   ```
   The tool installs config files (handling chattr immutability),
   syncs scripts + the shared library, seeds state files, and writes
   a backup tarball.
10. **Register host crons:**
    ```bash
    ssh openclaw@198.51.100.42 "~/repo/ops/scripts/install-host-cron.sh"
    ```
    Idempotent. Drift-detects and rewrites stale lines.
11. **Set the bot's slash-command menu and descriptions:**
    ```bash
    ssh openclaw@198.51.100.42 "bash ~/repo/ops/scripts/set-bot-commands.sh"
    ssh openclaw@198.51.100.42 "bash ~/repo/ops/scripts/set-bot-descriptions.sh"
    ```
    Add the new agent's command + description blocks to those scripts
    in step 2.
12. **Smoke-test:** send a `/ping` or `/status` to the bot. Confirm
    the next `*/15` fleet-health tick reports the new agent as `ok`.

For a **re-deploy** (updating an existing agent): skip steps 1-2, 6-7,
11. Just commit, push, pull, run `deploy.py`. Re-run
`install-host-cron.sh` only if cron schedules / script paths changed.

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
pattern; the per-agent heartbeat/keepalive host crons that used
this wrapper were later subsumed by the single `fleet-health`
host cron (R3, commit `949e99a`) which calls every agent's
`probe()` directly via `ops/scripts/probe-agent.py` and writes
`~/Dropbox/openclaw-backup/fleet-health.json`. The wrapper is
still the right pattern for per-agent SCRIPT_CONTRACT scripts
that shouldn't be aggregated into the fleet snapshot.

### 15. fix-it's cron-self-check will recreate stale entries in expected-crons.json (2026-04-14)

**Context:** Mr Fixit has a daily `cron-self-check` LLM cron
(`0 0 * * *`) that reads `~/.openclaw/fix-it-workspace/expected-crons.json`,
compares it against `openclaw cron list`, and re-registers any
"missing" entries. The instruction message only asked it to verify
the 9 fix-it-owned crons, but in practice the gpt-5.4 LLM reads the
whole file and re-creates anything it finds missing — across every
agent listed in that file.

**Symptom on 2026-04-14 at 00:01 UTC:** Telegram alert from Mr Fixit —
"🦊🔧 Cron repair: re-registered meetings-coach:heartbeat,
news-digest:heartbeat, news-digest:engagement-poll, shopping:heartbeat,
fix-it:heartbeat-check, fix-it:morning-status,
family-calendar:reminder-check, family-calendar:heartbeat". All 8 of
those crons had been *intentionally deleted* in the R3 fleet-health
consolidation (commit `5df91bd`) and moved to host cron via
`install-host-cron.sh`. But `expected-crons.json` still listed them.
Every midnight UTC, fix-it "repaired" them back into the LLM
gateway scheduler. Every next deployment removed them. Yo-yo.

**Worst of the duplicates:** `fix-it:morning-status` at `50 11 * * *`
vs the host-cron `morning-status-host.sh` at `30 10 * * *` — the operator
would get *two* morning status reports 80 minutes apart.

**Fix:** prune `agents/fix-it/expected-crons.json` (and the
`.example` template) of all entries that have been moved to host
cron. After today's cleanup the file lists:
- meetings-coach: pre-meeting-alert, post-meeting-scan, morning-meeting-brief, commitment-follow-up, weekly-review (5)
- news-digest: morning-edition, preference-update (2)
- shopping: costco-token-refresh, morning-delivery-brief, delivery-digest, subscribe-save-review (4)
- fix-it: cron-self-check, conflict-scan, brain-validation, security-audit, probation-end-reminder, file-size-monitor, obsidian-briefing, update-check, monthly-archival (9)
- family-calendar: whatsapp-chat-scan, activity-email-check, gmail-invite-check, morning-briefing, whatsapp-schedule-post, weekly-overview (6)

And the cron-self-check's message was rewritten to explicitly name
the 9 fix-it LLM crons *and* enumerate the host crons that are
**out of scope** (fleet-health, morning-status, reminder-check,
engagement-poll, costco-token-refresh, morning-fleet-deliver,
linkedin-keepalive) with a hard "do not re-register them even if
they look missing from openclaw cron list."

**Rule:** `expected-crons.json` is the ground truth for **LLM
gateway scheduler crons only**. Any cron moved to host cron (via
`install-host-cron.sh`) must be deleted from `expected-crons.json`
in the *same* commit — otherwise fix-it will silently yo-yo them
back in at midnight UTC and either duplicate work or waste LLM
cron budget.

**How to detect the yo-yo:** `docker exec openclaw-openclaw-gateway-1
openclaw cron list --json | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(j["createdAtMs"], j["agentId"], j["name"]) for j in sorted(d,key=lambda x:x["createdAtMs"])]'` — if a cluster of crons share a `createdAtMs` within a couple minutes of midnight UTC, Mr Fixit put them there.

### 16. Costco refresh_token grant needs the public client, not WCS (2026-04-14)

**Context:** `costco-token-daemon.py` has a five-level refresh
hierarchy: (0) cached id_token, (1) HTTP `refresh_token` grant via
`costco_refresh_headless.py`, (2) Hetzner silent-refresh in
Camoufox, (3) residential-proxy silent-refresh, (4) full credential
reauth. Level 1 is the fast path — a single HTTP POST to
`https://signin.costco.com/…/oauth2/v2.0/token` with
`grant_type=refresh_token`, ~1 second, no browser, no Akamai
contact. When it works, levels 2–4 become pure fallback and stay
idle for weeks at a time.

**Symptom on 2026-04-14 at 00:00 UTC:** Akamai's bot-reputation
scoring for Costco flipped at the midnight UTC policy boundary and
started serving "Access Denied" on `signin.costco.com` to *both*
the direct VPS IP and the dataimpulse residential proxy. Level 2
and level 3 began hanging for 120–240s per run, tripping the 120s
`costco-token-refresh-host.sh` wrapper timeout, filing
`exit_code=1` to `cache/last-token-refresh.json`, and firing the
daemon's "two consecutive failures" Telegram alert. Confirmed via
manual `--step hetzner` run: `[hetzner] Reload #1 (title='Access
Denied')` → `[hetzner] Reload #2 (title='Access Denied')` →
`Persistent Access Denied — exiting early`.

**Root cause (the real one, not the Akamai flip):** the level 1
fast path has been permanently starved since at least 2026-04-13
because `refresh_token` in `costco-tokens.json` was persistently
empty string. Every browser silent-refresh success called
`_success_exit` → `try_refresh_token_exchange(code)` → POST to
`/token` with `client_id=WCS_CLIENT_ID = 4900eb1f-...` (the
**confidential** Costco WCS client) → Costco returned
`AADB2C90079: Clients must send a client_secret when redeeming a
confidential grant` → returned `None` → `save_token(refresh_token=None)`
unconditionally truncate-wrote the file with `refresh_token=""`.
Net effect: every successful browser refresh wiped the fast-path
credential, forcing the next cron tick back through the browser
path that Akamai was about to start blocking.

**Fix (three commits):**

1. **Prevent the wipe.** `save_token` in `costco-token-daemon.py`
   rewritten as read-merge-write: if a caller passes
   `refresh_token=None`, preserve whatever is already in the file.
   Same pattern the `costco_refresh_headless._save_state` helper
   already uses. Covered by 5 new tests in `test_costco_token_daemon.py`
   including `test_save_token_preserves_refresh_token_when_none_passed`
   and `test_success_exit_preserves_existing_refresh_token`.

2. **Delete the dead exchange.** `try_refresh_token_exchange` and
   its call in `_success_exit` removed entirely — the confidential
   client cannot succeed here without a client_secret we do not
   have, so every call was a guaranteed 400 round-trip that polluted
   the logs and (pre-fix 1) wiped the credential.

3. **Bootstrap the refresh_token via the public client + PKCE.**
   `costco-pkce-probe.py` already implemented the correct flow,
   discovered and empirically GREEN-verified on 2026-04-12: the
   public B2C client `a3a5186b-7c89-4b4c-93a8-dd604e930757` accepts
   PKCE code_verifier + refresh_token grants against the same
   `B2C_1A_SSO_WCS_signup_signin_201` policy, without a
   client_secret. Added a `--bootstrap` flag that, on the first
   GREEN candidate, merges the captured refresh_token into
   `costco-tokens.json` via
   `costco_refresh_headless._save_state` (atomic .tmp rename).

**Run the bootstrap when the fast path is dead:**

```bash
docker exec openclaw-openclaw-gateway-1 python3 \
  /home/node/.openclaw/shopping-workspace/scripts/costco-pkce-probe.py \
  --bootstrap
```

Expected output:

```
[bootstrap] wrote refresh_token to …/costco-tokens.json (1535 chars)
GATE: GREEN
```

Precondition: the SSO cookie in `costco-session.json` must still
be valid (6-month TTL — should be fine unless it's been wiped).
The probe uses the saved cookies to silently capture the authorize
redirect without a credential prompt.

**Verification:** after bootstrap, run the daemon directly once
with a forced-stale id_token (backdate the `exp` claim) and
confirm:

```
[headless] Attempting refresh_token grant via public client...
[refresh-headless] SUCCESS — id_token N chars, rotated=True
SUCCESS via headless refresh_token grant (N chars)
real    0m1.165s
```

The full cycle — cached short-circuit *or* refresh_token grant —
must complete in under 2 seconds. If you see Camoufox launch,
something is wrong with the fast path and it's falling through to
levels 2+.

**Client-ID reference card (put this somewhere you can find it in
3 months when the refresh_token ages out):**

| Client ID | Type | Used for | Reference |
|---|---|---|---|
| `4900eb1f-0c10-4bd9-99c3-c59e6c1ecebf` | confidential (WCS) | Browser silent-refresh authorize URL via `make_authorize_url` — captures id_token in the OAuthLogonCmd form-POST. **Cannot** do `/token` exchange. | `WCS_CLIENT_ID` in `costco-token-daemon.py` |
| `a3a5186b-7c89-4b4c-93a8-dd604e930757` | public (PKCE) | HTTP-only refresh_token grants at `/oauth2/v2.0/token`. **Required** for level 1 fast path. | `PUBLIC_CLIENT_ID` in `costco_refresh_headless.py`; candidates list in `costco-pkce-probe.py` |

Don't try to exchange a WCS-issued authorization code with the
public client — codes are client-bound and AADB2C will reject it.
The bootstrap flow issues a fresh PKCE code against the public
client; the browser silent-refresh path stays on the WCS client
for its id_token capture only.

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
oc() { docker compose -f ~/repo/ops/docker-compose.yml exec -T openclaw-gateway openclaw "$@"; }
oci() { docker compose -f ~/repo/ops/docker-compose.yml exec -it openclaw-gateway openclaw "$@"; }
```

The `~/openclaw/docker-compose.yml` path also works because post-
Phase-3b-followup it is a symlink to `~/repo/ops/docker-compose.yml`.
Either path resolves to the same git-tracked compose file; prefer
the `~/repo/ops/` form in fresh configs so the runtime and git
source of truth are visibly the same place.

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

See `guide-v1/09-cli-reference.md` for the full historical reference (frozen, OpenClaw-era).
