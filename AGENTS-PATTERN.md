# Agent Development Patterns

Standard patterns all Busytown agents must follow. Read this before building a new agent.

## Git

- **Only Mr Fixit pushes to GitHub.** All other agents can `git commit` to the repo at `~/repo/` but NEVER `git push`.
- **Commit to your own directory only.** Lowly Worm commits to `agents/news-digest/`, Rudolf to `agents/rudolf/`, etc.
- **Mr Fixit runs a pre-push safety check** (`scripts/pre-push-check.sh`) before every push — scans for secrets, .env files, large files, empty commit messages.
- **Never commit secrets.** API keys, bot tokens, passwords stay in `.env` (gitignored).

## Identity

- Each agent has a Busytown character (see README.md for the roster)
- Identity files: SOUL.md, IDENTITY.md, TOOLS.md, CRONS.md in `agents/{agent-name}/`
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

See [DEPLOY.md](DEPLOY.md) for the full step-by-step. The pattern:
1. Create Telegram bot
2. Write SOUL.md, IDENTITY.md, TOOLS.md, CRONS.md
3. Write deploy.sh
4. SCP to VPS, create agent, run deploy script
5. Pair Telegram bot
6. Security hardening (chattr)
7. Run test suite
