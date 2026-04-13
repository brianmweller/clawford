# Known Issues — Mr Fixit Alert Suppression

> Entries here suppress matching alerts in the morning status report.
> Each entry has: match pattern (regex or substring matched against agent status text),
> expires date (YYYY-MM-DD — auto-ignored after this date), reason, and escalation conditions.
> Remove entries once the underlying issue is resolved.

---

- **match:** meetings-coach.*multi-writer|meetings-coach.*stale header
- **expires:** 2026-05-10
- **reason:** Status file multi-writer bloat — fix specified (heartbeat sole writer) but not yet enforced in agent code. Tracked.
- **escalation:** If status file exceeds 50KB or heartbeat stops writing entirely.

---

- **match:** tools\.exec\.security.*full|exec.*policy.*full
- **expires:** 2027-01-01
- **reason:** policy=full is intentional for all agents. OpenClaw allowlist only matches binary paths; LLM compound commands (pipes, &&, redirections) require full mode. Single-operator setup, no untrusted agents. Not a finding.
- **escalation:** If an untrusted or third-party agent is added with policy=full.
