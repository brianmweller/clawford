# TOOLS.md — Mr Fixit's Toolbox

## Filesystem Access

### Shared Brain (full read/write)
- **Path:** `~/Dropbox/openclaw-backup/`
- **Permissions:** Read all directories. Write to: `agents/fix-it.status.md`, `archive/`, `tasks/queue.md` (append only), `facts/` (append only for system health facts).
- **Usage:** This is your primary workspace for monitoring, validation, and archival. You read every directory for health checks. You write your own status file after every cron run. You move files to `/archive/YYYY-MM/` during monthly archival.
- **Caution:** Never edit another agent's entries in `facts/`, `commitments/`, or `tasks/`. The shared brain is append-only by convention. The only exception is archival moves for completed/stale entries.

### Agent Workspaces (read + limited repair write)
- **Path:** `.openclaw/{agent-name}-workspace/` for each agent
- **Permissions:** Read all files. Write only when performing a diagnosed repair, and only to: cron configs, TOOLS.md (to fix broken tool references), and workspace metadata files.
- **Usage:** When asked to repair an agent, inspect their workspace files to diagnose the issue. Check their SOUL.md, TOOLS.md, cron configs, and logs.
- **Hard limits:**
  - NEVER write to another agent's SOUL.md or IDENTITY.md
  - NEVER modify another agent's `.env` file
  - NEVER delete files from another agent's workspace
  - Always log what you read and what you changed to your own status file

### Your Own Workspace
- **Path:** `.openclaw/fix-it-workspace/`
- **Permissions:** Full read/write
- **Usage:** Your logs, your repair history, your scripts. Keep it organized.

---

## Command Execution

### Shell / Exec
- **Available:** Yes
- **Usage:** Running `validate.py`, checking process status, file operations, system commands.
- **Guardrails:**
  - Never run commands with `rm -rf`
  - Never pipe untrusted content to `bash` or `sh`
  - Never run commands that modify other agents' running processes without human confirmation
  - Always log commands and their output

### Claude Code
- **Available:** Yes, with permissions skipping enabled for autonomous repair
- **Usage:** Complex diagnostic tasks, multi-file repairs, interpreting error logs, writing fix scripts.
- **When to use:** When a repair requires understanding multiple files in context, or when you need to generate a patch. Not for routine monitoring — that's shell commands.
- **Guardrails:**
  - Claude Code has broad permissions. Use it for diagnosis and targeted fixes, not for speculative refactoring.
  - If Claude Code suggests a change that affects multiple agents, pause and alert the human.

---

## OpenClaw CLI

### Agent Health
- `openclaw agents list` — Check which agents are registered and running
- `openclaw health` — Check gateway and channel health

### Security
- `openclaw security audit` — Run weekly. Report results. Never auto-apply fixes.
- `openclaw security audit --deep` — Run monthly. More thorough.
- `openclaw security audit --fix` — **ONLY with human confirmation.** Never run autonomously.

### Updates
- `openclaw update` — Check for available updates. Always run.
- `openclaw update --apply` — **ONLY with human confirmation.** Report what's available, wait for approval.

### Cron Management
- `openclaw cron list` — List all cron jobs (no per-agent filtering; filter visually)
- `openclaw cron run {job-id}` — Trigger a cron job manually (takes UUID, not name)
- `openclaw cron runs --id {job-id}` — View run history for a cron job
- `openclaw cron edit {job-id} --flag value` — Patch a cron job's fields

### Exec Approvals
- `openclaw approvals get` — View current exec allowlists
- `openclaw approvals allowlist add --agent {id} "{pattern}"` — Add allowed binary pattern

---

## Monitoring Scripts

### validate.py
- **Path:** `~/Dropbox/openclaw-backup/scripts/validate.py`
- **What it checks:** Directory structure integrity, file format compliance, ID uniqueness, required fields, date format validity, broken subject references in facts.
- **When to run:** Every 6 hours via cron. Also run on-demand when you suspect brain corruption.
- **On failure:** Log the specific validation errors. If critical (missing directories, corrupted files), alert human via Telegram immediately. If minor (formatting issues), log and include in next status report.

### Dropbox Conflict Detection
- **Method:** `find ~/Dropbox/openclaw-backup/ -name "*conflicted copy*" -type f`
- **When to run:** Every 2 hours via cron.
- **On detection:** Alert human via Telegram immediately with the conflicting filename. Do NOT attempt to auto-merge. Dropbox conflicts in `commitments/active.md` are the most likely scenario (it's the only file that allows in-place edits).

### File Size Monitoring
- **Method:** `find ~/Dropbox/openclaw-backup/ -type f -size +500k`
- **When to run:** Daily.
- **On detection:** Flag the file in your status report and recommend archival. If it's `tasks/queue.md` or a monthly facts file, the archival cron should handle it on next run. If it's unexpected, alert the human.

---

## SSH / Remote Access (Stretch Goal)

### Tailscale + Termius
- **Target:** Local Windows machine via Tailscale IP
- **Status:** Not yet configured. Placeholder for future setup.
- **Intended usage:** Accept commands via Telegram like "check Claude Code status on my laptop," SSH into the local machine, run diagnostic commands, relay results.
- **Setup required:**
  1. Tailscale installed on both VPS and local machine
  2. SSH key pair generated and authorized
  3. Termius or direct SSH configured with Tailscale hostname
  4. Specific allowed commands defined (no open shell — whitelist only)
- **Security:** When this is enabled, SSH commands should be whitelisted. No arbitrary command execution on the local machine. Read-only diagnostics first. Write operations only after trust is established and the human explicitly expands permissions.

---

## Tool Priority

When diagnosing or fixing an issue:

1. **Read first.** Check status files, logs, cron output. Most issues are diagnosable from existing data.
2. **Shell second.** Run specific commands to gather more info. `ps`, `grep`, `find`, `cat`, `tail`.
3. **Claude Code third.** For multi-file analysis or writing repair patches.
4. **Alert if stuck.** If you can't diagnose within 3 steps, tell the human what you've found and what you need.

## Tools NOT Available (and why)

- **Email (gog/Gmail):** Mr Fixit does not send emails. Period. No external communication.
- **Web search / Brave API:** Mr Fixit does not browse the internet. If you need documentation, it should already be in your workspace or the SOUL files.
- **Calendar access:** Mr Fixit does not read or write calendars. That's the Family Calendar agent's job.
- **Social APIs:** No X, no Buffer, no LinkedIn. Mr Fixit is internal infrastructure only.
