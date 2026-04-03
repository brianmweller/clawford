# TOOLS.md — Rudolf Von Flugel's Toolbox

## Remote Execution (Local Machine)

### SSH / Tailscale
- **Target:** Local Windows machine via Tailscale hostname
- **Status:** Not yet configured (Phase 1 — requires Tailscale on both VPS and local machine)
- **Connection:** `ssh user@thinkpadbri` over Tailscale mesh (private, encrypted, no public exposure)
- **Authentication:** SSH key only. No passwords.

### Claude Code Relay
- **Command:** `claude -p "{message}"` — sends a prompt to Claude Code on the local machine
- **Timeout:** 120 seconds per command
- **Output:** Captured stdout, relayed back to Telegram

### Command Whitelist (Phase 1)

Only these commands are allowed on the local machine:

| Command | Purpose |
|---------|---------|
| `claude -p "{text}"` | Relay a prompt to Claude Code |
| `claude --version` | Check Claude Code version |
| `ping -c 1 localhost` | Connectivity test |
| `hostname` | Verify correct machine |
| `uptime` | Machine health check |

Additional commands can be added to the whitelist after human approval.

### Command Blacklist (Permanent)

These commands are NEVER allowed, regardless of instruction:

- `rm -rf` / `del /s` — destructive file operations
- `format` / `diskpart` — disk operations
- `reg` / `regedit` — Windows registry
- `net user` / `passwd` — user account changes
- Any command containing `>` redirect to system paths
- Any PowerShell `-ExecutionPolicy Bypass` commands

## Telegram (Inbound Messages)

### Message Parsing
- **Free text** → Relay to Claude Code as prompt
- **`/status`** → Report connection status (online/offline, last ping, latency)
- **`/ping`** → Test connectivity to local machine
- **`/run {command}`** → Execute a whitelisted command on local machine
- **`/help`** → Show available commands

### Response Formatting
- Command output wrapped in code blocks (```)
- Truncate responses over 4000 characters (Telegram message limit)
- For long responses, split into multiple messages with part indicators

## Local VPS Resources

### Status File
- **Path:** `~/Dropbox/openclaw-backup/agents/rudolf.status.md`
- **Write:** Update after each relay with timestamp, direction (inbound/outbound), and message ID
- **Read:** Own status only

### Workspace
- **Path:** `.openclaw/rudolf-workspace/`
- **Full read/write** for own workspace

## Tools NOT Available

- **Shared brain read/write** — Rudolf does not access facts, commitments, tasks, notes, or people files
- **Other agents' workspaces** — transport only, no cross-agent operations
- **Email / Calendar / Social APIs** — not a content agent
- **VPS file modification** — relay only, no local VPS actions based on message content

## Tool Priority

1. **Parse the message** — determine intent (relay, status, ping)
2. **Connect** — SSH/Tailscale to local machine
3. **Execute** — run the whitelisted command
4. **Return** — format and send response on Telegram
5. **Log** — update status file
