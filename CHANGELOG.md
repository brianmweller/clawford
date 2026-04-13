# Changelog

## 0.3.0 — 2026-04-05

### Added
- Local Telegram relay bot (`telegram-relay/bot.py`) — bridges Telegram to local Claude Code CLI
- Voice message support via Whisper transcription
- `/ping`, `/status`, `/cwd` bot commands

### Removed
- Rudolf Von Flugel agent — replaced by local relay bot (no VPS needed)

### Changed
- Agent roster reduced from 7 to 6
- Guide updated: deployment order, Tailscale no longer required for relay

## 0.2.0 — 2026-04-03

### Added
- 10-chapter setup guide (`guide/`)
- Busytown character theme for all agents
- Rudolf Von Flugel agent spec (Telegram ↔ local Claude Code relay)
- Dynamic agent discovery in `validate.py` (no more hardcoded agent list)
- VERSION and CHANGELOG files

### Changed
- Migrated VPS to Docker-based deployment (Terraform + docker-compose)
- DEPLOY.md rewritten for Docker workflow (`oc()` wrapper)
- deploy.sh updated with Docker exec, chattr hardening, `/usr/local/bin/*` allowlist
- Test harness updated for Docker (`oc()` function, timestamp-based polling)
- README updated with Busytown roster (7 agents)
- Python3 added to Docker image for validate.py

### Fixed
- Test T2 polling race condition (timestamp-based run matching)
- Test T5 stale fact injection (inject into active file, not separate file)
- validate.py no longer fails when agents are added or removed

## 0.1.0 — 2026-04-02

### Added
- Initial project structure (agents/, brain/, docs/, tests/)
- Shared brain schema and setup script (8 dirs, 16 seed files)
- Mr Fixit agent (SOUL, IDENTITY, TOOLS, CRONS, deploy script)
- 9 scheduled cron jobs for Mr Fixit
- Test harness with 6 tests (T1-T6)
- Per-agent Telegram bot pattern
- Security hardening (chattr +i for SOUL/IDENTITY files)
- Exec allowlist for agent shell access
- GitHub repo (private, your own handle)
