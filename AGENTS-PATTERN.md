# Agent Development Patterns

Standard patterns all Busytown agents must follow. Read this before building a new agent.

## Git

- **Only Mr Fixit pushes to GitHub.** All other agents can `git commit` to the repo at `~/repo/` but NEVER `git push`.
- **Commit to your own directory only.** Lowly Worm commits to `agents/news-digest/`, Hilda to `agents/shopping/`, etc.
- **Mr Fixit runs a pre-push safety check** (`scripts/pre-push-check.sh`) before every push — scans for secrets, .env files, large files, empty commit messages.
- **Never commit secrets.** API keys, bot tokens, passwords stay in `.env` (gitignored).

## Memory

Every agent MUST have a `MEMORY.md` in its workspace. OpenClaw auto-loads it at the start of every session.

- **MEMORY.md** — persistent rules, hard constraints, architectural decisions. Things the agent must remember across sessions.
- **memory/YYYY-MM-DD.md** — daily notes (auto-created by OpenClaw). Today + yesterday loaded automatically.
- **Memory flush** — before compaction, OpenClaw reminds the agent to save context. On by default.
- **Dreaming** — optional consolidation that promotes daily notes to MEMORY.md.

What goes in MEMORY.md:
- Config rules: "NEVER re-enable X" / "ALWAYS use Y"
- Architectural decisions: why something was done a certain way
- Learned patterns: what works, what breaks
- Do NOT put ephemeral task state — that goes in daily files

## Workspace Files (All 8 Required)

OpenClaw auto-loads exactly 8 files at every session start. ALL must exist in the agent's workspace:

| File | Purpose | Create at deploy? |
|------|---------|-------------------|
| SOUL.md | Personality, values, boundaries, operating model | Yes (immutable via chattr) |
| IDENTITY.md | Name, emoji, tone, catchphrase | Yes (immutable via chattr) |
| TOOLS.md | Available tools, permissions, commands | Yes |
| AGENTS.md | Hard rules, role, config architecture, agent roster | Yes |
| USER.md | Human's name, timezone, preferences, communication style | Yes |
| HEARTBEAT.md | 30-minute checklist (lightweight recurring checks) | Yes |
| MEMORY.md | Persistent lessons, hard constraints learned from experience | Seed at deploy, agent maintains |
| BOOTSTRAP.md | First-run onboarding (DELETE after initial setup) | No (delete if present) |

**If any of these are missing or generic, the agent won't know its role, its rules, or its human.**

## Identity
- SOUL.md and IDENTITY.md are made immutable on the VPS after deployment (`chattr +i`)
- Each agent gets its own Telegram bot via @BotFather

## Shared Brain

- Lives at `~/Dropbox/openclaw-backup/` (synced via Dropbox)
- Agents write to their own status file (`agents/{name}.status.md`)
- Append-only convention — never overwrite another agent's entries
- Mr Fixit monitors all status files and validates brain health

## Claude Code

- Only Mr Fixit uses Claude Code (via `claude -p` shell command, NOT ACP)
- Other agents use OpenClaw's native LLM capability
- `--add-dir ~/Dropbox/openclaw-backup/` required for brain access
- Multi-turn: `--session-id $(uuidgen)` on turn 1, `--resume` on subsequent turns
- New request = new session ID. Never reuse across requests.

## Telegram

- Each agent's bot must be the `default` Telegram account (token via `TELEGRAM_BOT_TOKEN` env var)
- Named accounts show "not configured" and don't receive inbound messages
- Routing: bind the agent to `accountId: "default"`
- Silent crons: use `--no-deliver --failure-alert` for routine checks
- Noisy crons: use `--announce` for reports the human always wants

## Testing

- Test harness at `~/openclaw-tests/`
- Each agent gets tests in `tests/{agent-name}/`
- Convention: `__TEST__` prefix for all test fixtures
- Red/green TDD: write tests first, confirm fail, implement, confirm pass

## Security

- `chattr +i` on SOUL.md and IDENTITY.md after deployment
- Exec allowlist for cron commands
- Telegram exec approvals for interactive commands (Mr Fixit only)
- Secrets in `.env` only, never in code or brain
- ACP disabled (`acp.enabled: false`) — ACP hijacks Telegram channels

## Deployment

See [DEPLOY.md](DEPLOY.md) for the full step-by-step. The pattern
(post 2026-04-12 refactor):

1. Create Telegram bot via @BotFather
2. Write workspace files (SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md,
   USER.md, HEARTBEAT.md, MEMORY.md, CRONS.md) locally in the Clawford
   repo
3. Write a `manifest.json` next to the workspace files (or generate
   one from a legacy `deploy.sh` via
   `agents/shared/import_from_deploy_sh.py`)
4. **Commit everything locally and push to GitHub.** No SCP-bypass.
5. On the VPS: `cd ~/repo && git pull`
6. Interactive onboarding (first time only):
   `oci agents add <agent-id>`, then `/start` the bot and approve
   pairing
7. Deploy: `python3 agents/shared/deploy.py <agent-id>`
   (the tool handles file install, cron registration, channel
   binding, approvals, chattr locking, and backup)

## Deployment Invariants

These invariants are enforced by `agents/shared/deploy.py` and
documented in the three memory entries linked in the canonical memory
index. Violating them requires an explicit override flag, which logs a
warning (or an audit record in the case of drift violations).

1. **Local git is the source of truth.** The VPS workspace is
   ephemeral — it gets rebuilt from local git on every deploy. Never
   edit a workspace file directly on the VPS; if you do, commit it
   back to local git before the next deploy runs or your edit will
   be lost.
2. **Every deploy is preceded by a clean git commit.** `deploy.py`
   refuses to run if the agent's source directory has uncommitted
   modifications or untracked files. Override: `--allow-dirty`.
3. **Every deploy produces a backup tarball** at
   `~/.openclaw/deploy-backups/<agent>-<ts>.tar.gz` AND mirrors it
   to `~/Dropbox/openclaw-backup/deploy-backups/` for off-VPS
   retention. Recovery from a bad deploy is `tar -xzf`.
4. **Every UPDATE is reviewed via diff before landing.** The tool
   prints a unified diff for each changed file and waits for y/N.
   Override: `--yes-updates`.
5. **Workspace drift between deploys is a blocking error.** If the
   workspace has changed since the last recorded manifest, the next
   deploy refuses. Override: `--accept-drift` (logs a violation).
6. **Infrastructure code is built test-first.** See
   `feedback_tdd_mandatory_for_infra.md` memory — the deploy tool
   itself, any backup scripts, any cron editors — all built red/green.

## Deployment workflow rules (human-facing)

- NO ON-VPS DEV. Edits happen in local git, full stop.
- Never `scp` files directly into `~/repo/` on the VPS. Use `git pull`.
- Never hand-write cron messages with `chr()`, compound shell pipes,
  or heredocs — OpenClaw's exec layer can trip approval flows even
  under `policy=full, ask=off`. Use Python scripts invoked via
  `python3 /home/node/.openclaw/<agent>-workspace/scripts/<name>.py`.
- Never commit secrets. API keys, bot tokens, passwords live in
  `.env` (gitignored) and are sourced into deploy environment at
  runtime.
