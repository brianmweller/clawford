# HEARTBEAT.md — Lowly Worm's 30-Minute Checklist

This file is read by OpenClaw every 30 minutes during the heartbeat cycle.

## Every Heartbeat (30 min)

1. **Update your status file** with the current timestamp:
   - Write `last_heartbeat` to `/home/node/Dropbox/openclaw-backup/agents/news-digest.status.md`

2. **Check feed health:** If the last cron run had feed failures, note them in the status file.

3. **No output unless there's a problem.** Silent heartbeats. Only alert on Telegram if feeds are down or the cron hasn't run in over 24 hours.

## Do NOT do during heartbeat

- Do NOT fetch RSS feeds (that's the morning cron's job)
- Do NOT deliver news (scheduled delivery only)
- Do NOT scrape LinkedIn (once daily during morning cron only)
- Do NOT modify preference model
- Do NOT run Git operations
