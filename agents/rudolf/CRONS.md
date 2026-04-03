# CRONS.md — Rudolf Von Flugel Cron Schedule

Rudolf is primarily **reactive** (responds to Telegram messages), not cron-driven like Mr Fixit. However, he has a few lightweight scheduled jobs.

Agent: rudolf
Workspace: .openclaw/rudolf-workspace/

---

## Connection Health Check — Every 15 Minutes

**Schedule:** `*/15 * * * *`
**Command:** Ping the local machine via Tailscale. Update status file with connectivity status.

**On success:** Update `rudolf.status.md` with `connection: online`, latency, timestamp. Silent.
**On failure:** Update status with `connection: offline`. If offline for >30 minutes, alert human on Telegram: "✈️ Control tower: local machine has been offline for 30+ minutes."

**Telegram output:** Only on prolonged outage (>30 min).

---

## Status Heartbeat — Every 30 Minutes

**Schedule:** `*/30 * * * *`
**Command:** Update own heartbeat in `rudolf.status.md`. Check relay log for errors.

**Telegram output:** Only on failure.

---

## Summary Table

| Cron | Frequency | Telegram | Purpose |
|------|-----------|----------|---------|
| Connection health | Every 15 min | On prolonged outage | Monitor local machine reachability |
| Status heartbeat | Every 30 min | On failure only | Self-health reporting |

---

## Note: Message Relay is NOT a Cron

The core function (receiving Telegram messages and relaying to Claude Code) is handled by OpenClaw's real-time message routing, not by cron jobs. When a Telegram message arrives at Rudolf's bot, OpenClaw routes it to the rudolf agent, which processes it immediately.
