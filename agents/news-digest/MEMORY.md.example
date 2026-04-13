# Lowly Worm — Persistent Memory

Rules learned from experience. Read this before making changes.

## Git: commit, never push
- Commit your own code changes to `agents/news-digest/` in `~/repo/`
- Mr Fixit is the only agent that pushes to GitHub
- Never run `git push`

## LinkedIn: read-only, once daily
- Playwright browser scrape via `linkedin-scrape.py`
- Session saved in `linkedin-profile/`
- If session expires, skip LinkedIn section and alert on Telegram
- NEVER write to LinkedIn (post, comment, like, connect, message)

## Feed fetching: once per cron run
- Cache results for on-demand reuse within 1 hour
- Don't re-fetch during heartbeat
- Track seen items in dedup files to avoid repeats
