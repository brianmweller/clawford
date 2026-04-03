# Chapter 9: CLI Reference

The official OpenClaw CLI docs are wrong in several places. Trust this chapter.

---

All commands below use the Docker exec wrapper. Define these in your SSH session or `~/.bashrc`:

```bash
oc() { docker compose -f ~/openclaw/docker-compose.yml exec -T openclaw-gateway openclaw "$@"; }
oci() { docker compose -f ~/openclaw/docker-compose.yml exec -it openclaw-gateway openclaw "$@"; }
```

`oc` is for non-interactive commands (scripts, crons). `oci` is for interactive commands (agent onboarding).

---

## Command Reference

### Gateway & Health

| Command | Description |
|---------|-------------|
| `oc health` | Check gateway, Telegram, and agent status |
| `cd ~/openclaw && docker compose up -d` | Start the container |
| `cd ~/openclaw && docker compose restart` | Restart the container |
| `cd ~/openclaw && docker compose logs --tail 20` | View recent logs |
| `cd ~/openclaw && docker compose build --no-cache && docker compose up -d` | Rebuild image and restart |

### Agents

| Command | Description |
|---------|-------------|
| `oci agents add {name}` | Create a new agent (interactive) |
| `oc agents list` | List all registered agents |
| `oc agents bind --agent {id} --bind telegram:{account}` | Bind agent to a Telegram bot |
| `oc agents bindings` | Show all routing bindings |
| `oc agents delete {name}` | Remove an agent |

### Cron Jobs

| Command | Description |
|---------|-------------|
| `oc cron add --agent {id} --name "{name}" --cron "{expr}" --message "{text}" --to {chatId} --account {agent-id} --announce` | Register a cron job |
| `oc cron list` | List all cron jobs |
| `oc cron run {job-uuid}` | Trigger a cron manually |
| `oc cron runs --id {job-uuid}` | View run history |
| `oc cron edit {job-uuid} --message "{new text}"` | Edit a cron job |
| `oc cron rm {job-uuid}` | Remove a cron job |

### Telegram Channels

| Command | Description |
|---------|-------------|
| `oc channels add --channel telegram --token {token} --account {id} --name "{Name}"` | Add a bot as a channel account |
| `oc channels list` | List configured channels |
| `oc pairing approve telegram {CODE}` | Approve Telegram bot pairing |
| `oc pairing list` | List pending pairing requests |

### Exec Approvals

| Command | Description |
|---------|-------------|
| `oc approvals get` | View current allowlists |
| `oc approvals allowlist add --agent {id} "{pattern}"` | Add an allowed binary pattern |
| `oc approvals allowlist remove --agent {id} "{pattern}"` | Remove a pattern |

### Device Pairing

| Command | Description |
|---------|-------------|
| `oc devices list` | Show pending and paired devices |
| `oc devices approve {request-id}` | Approve a pending device |

---

## The 15 Pitfalls

Every one of these was learned by hitting the error and debugging it.

### 1. `cron` not `crons`
```
error: unknown command 'crons'
```
The subcommand is `openclaw cron`, singular. Not `crons`.

### 2. `--cron` not `--schedule`
The flag for the cron expression is `--cron "{expr}"`, not `--schedule`.

### 3. `--message` not `--prompt`
The flag for the agent instruction is `--message "{text}"`, not `--prompt`.

### 4. `--tools` causes API errors
```
unexpected property 'toolsAllow'
```
Do NOT use the `--tools` flag on `cron add`. It exists in the help but causes a gateway API error. Omit it entirely.

### 5. `cron run` takes UUID, not name
```
Error: unknown cron job id: heartbeat-check
```
Get the UUID from `oc cron list`, then: `oc cron run {uuid}`.

### 6. `cron rm` takes UUID or name, no `--name` flag
```
error: unknown option '--name'
```
Use: `oc cron rm {uuid}` or `oc cron rm {name}` (bare argument, no flag).

### 7. `cron list` has no `--agent` filter
```
error: unknown option '--agent'
```
`oc cron list` shows all crons. Filter visually by the Agent ID column.

### 8. `openclaw agents config` does not exist
There is no `agents config` subcommand. For exec permissions, use `oc approvals allowlist add`.

### 9. Telegram delivery requires three flags
Every cron must include `--to {chatId} --account {agent-id} --announce`. Omitting any one causes silent delivery failure — the cron runs, the agent works, but no Telegram message arrives.

### 10. Device pairing blocks everything
```
GatewayClientRequestError: pairing required
```
Run `oc devices list` and approve any pending requests with `oc devices approve {id}`.

### 11. Gateway must be running
All `oc` commands fail if the container is down. Check: `oc health`. Fix: `cd ~/openclaw && docker compose up -d`.

### 12. Exec denied in cron context
```
exec denied for heartbeat inspection commands
```
Add binary patterns to the exec allowlist: `oc approvals allowlist add --agent {id} "/usr/bin/*"`.

### 13. Python3 not in container
The default Docker image is Node-only. If `validate.py` fails to run, add `python3` to the Dockerfile and rebuild.

### 14. Windows line endings in scripts
```
/usr/bin/env: 'bash\r': No such file or directory
```
SCP from Windows adds `\r`. Fix: `sed -i 's/\r$//' ~/openclaw/entrypoint.sh`

### 15. Brain not mounted in Docker
Agents inside Docker can't see `~/Dropbox/openclaw-backup/` unless it's a volume mount. Add to `docker-compose.yml`:
```yaml
- /home/openclaw/Dropbox/openclaw-backup:/home/node/Dropbox/openclaw-backup
```

---

## Diagnostic Workflow

When something isn't working:

```bash
# 1. Is the container running?
cd ~/openclaw && docker compose ps

# 2. Is the gateway healthy?
oc health

# 3. Is the agent registered?
oc agents list

# 4. Are crons registered?
oc cron list

# 5. What did the last cron run produce?
oc cron runs --id {job-uuid}

# 6. Is the exec allowlist configured?
oc approvals get

# 7. Are there pending device pairings?
oc devices list

# 8. What do the container logs say?
docker compose logs --tail 50
```

## Rollback

```bash
# Remove crons (by UUID)
oc cron rm {uuid-1}
oc cron rm {uuid-2}
# ... one per cron

# Remove the agent
oc agents delete {agent-name}

# Brain is untouched — agents only append, never destructively edit
```

---

Back to [Index](index.md)
