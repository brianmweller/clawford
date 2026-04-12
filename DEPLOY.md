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

For a brand-new agent that has never been onboarded:

1. Create the Telegram bot via @BotFather, save token in VPS `~/openclaw/.env`
2. Write SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, USER.md, HEARTBEAT.md,
   MEMORY.md, CRONS.md in `agents/<new-agent>/`
3. Write the agent's scripts under `agents/<new-agent>/scripts/`
4. Write a `manifest.json` (or generate from a skeleton `deploy.sh` via
   `import_from_deploy_sh.py`)
5. Commit everything to local git and push
6. On the VPS: `oci agents add <new-agent>` (interactive onboarding —
   can't be automated by `deploy.py` yet)
7. `/start` the bot on Telegram, `oc pairing approve telegram <CODE>`
8. `cd ~/repo && git pull && python3 agents/shared/deploy.py <new-agent>`

For a RE-deploy (updating an existing agent): skip steps 1-3 and 6-7.
Just commit, push, pull, deploy.

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
