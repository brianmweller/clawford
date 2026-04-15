# Agent Development Patterns

Standard patterns all Busytown agents must follow. Read this before building a new agent.

## Git

- **Only Mr Fixit pushes to GitHub.** All other agents can `git commit` to the repo at `~/repo/` but NEVER `git push`.
- **Commit to your own directory only.** Lowly Worm commits to `agents/news-digest/`, Hilda to `agents/shopping/`, etc.
- **Mr Fixit runs a pre-push safety check** (`scripts/pre-push-check.sh`) before every push — scans for secrets, .env files, large files, empty commit messages.
- **Never commit secrets.** API keys, bot tokens, passwords stay in `.env` (gitignored).

## Memory

Every agent has a `MEMORY.md` in its workspace. Cron messages and the
LLM cron runner load it explicitly when they need persistent context
— there is no auto-loading runtime layer post-Phase-6. The file is
durable across runs because it lives in the workspace, not in
session memory.

- **MEMORY.md** — persistent rules, hard constraints, architectural decisions. Things the agent must remember across cron invocations.
- **memory/YYYY-MM-DD.md** — daily notes when an agent wants to write them. Loaded explicitly by the consumer.

What goes in MEMORY.md:
- Config rules: "NEVER re-enable X" / "ALWAYS use Y"
- Architectural decisions: why something was done a certain way
- Learned patterns: what works, what breaks
- Do NOT put ephemeral task state — that goes in daily files

## Workspace Files

The eight workspace files below describe an agent's identity, role,
and operating model. They are loaded on demand by cron message
prompts (and by `llm-cron-runner.py` when applicable), not auto-
injected by a runtime layer. ALL must exist in the agent's workspace
for the agent's prompts to resolve correctly:

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

## LLM access

- Every agent that needs LLM reasoning calls
  `from agents.shared.llm import infer` — a thin wrapper over
  `codex infer` riding the operator's ChatGPT Plus subscription. Zero
  marginal cost per call.
- `infer(prompt, *, json_mode=False, timeout=30, model=None)` returns
  an `InferResult` with normalized `.text` and `.outputs` fields.
- Mr Fixit also uses Claude Code (`claude -p`) for richer fleet-wide
  diagnostics. Pass `--add-dir ~/Dropbox/openclaw-backup/` for brain
  access. Multi-turn: `--session-id $(uuidgen)` on turn 1, `--resume`
  on subsequent turns.

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
- Per-agent Telegram bots prevent cross-agent impersonation
- Secrets in `.env` only (`~/clawford/.env` on the VPS), never in
  code or brain
- Three-tier defense: OS-level immutability on identity files, the
  script contract for cron messages (no shell operators in any
  message string), and `deploy.py`'s ten safeguards. See
  `guide-v3/06-infra-setup.md`.

## Deployment

See [DEPLOY.md](DEPLOY.md) for the full step-by-step. The pattern
(post-Phase-7 liberation):

1. Create Telegram bot via @BotFather
2. Write workspace files (SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md,
   USER.md, HEARTBEAT.md, MEMORY.md, CRONS.md) locally in the Clawford
   repo
3. Write a `manifest.json` next to the workspace files
4. **Commit everything locally and push to GitHub.** No SCP-bypass.
5. SSH to the VPS: `cd ~/repo && git pull --ff-only origin master`
6. Deploy: `python3 agents/shared/deploy.py <agent-id> --yes-updates`
   (file install + chattr handling + backup tarball + Dropbox mirror).
7. Register host crons: `~/repo/ops/scripts/install-host-cron.sh`
   (idempotent — drift-detects and rewrites stale lines).

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
   `~/.clawford/deploy-backups/<agent>-<ts>.tar.gz` AND mirrors it
   to `~/Dropbox/openclaw-backup/deploy-backups/` for off-VPS
   retention. Recovery from a bad deploy is `tar -xzf`. (The
   Dropbox mirror path keeps the legacy `openclaw-backup` name to
   avoid resetting Dropbox sync history fleet-wide.)
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
  or heredocs. Use Python scripts invoked via
  `python3 /home/openclaw/.clawford/<agent>-workspace/scripts/<name>.py`
  through `script-contract-host.sh` or a dedicated `*-host.sh`
  wrapper. The script contract guarantees one JSON line on stdout;
  the wrapper parses it and decides whether to alert.
- Never commit secrets. API keys, bot tokens, passwords live in
  `.env` (gitignored) and are sourced into the wrapper environment
  at runtime from `~/clawford/.env`.
