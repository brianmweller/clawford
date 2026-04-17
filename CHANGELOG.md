# Changelog

## 1.0.0 — 2026-04-15 — Liberation

The Clawford liberation: every load-bearing OpenClaw dependency
removed. The fleet now runs on plain host crons invoking Python
scripts directly via `/usr/bin/python3`, with a from-scratch
`agents/shared/` library and a custom `deploy.py`. Major version cut
because the runtime substrate changed underneath.

### Phase 0 — Plan + manifesto

- New `guide-v3/` directory scaffolded as the post-liberation field
  manual. Old `guide-v2/` frozen as historical record.
- New chapter `guide-v3/02-what-isnt-clawford.md` drafted up front as
  a decision document.
- Per-chapter migration tracker at `docs/v2-to-v3-migration.md`.

### Phase 1 — codex auth + LLM backend shim

- `agents/shared/llm.py` introduced with backend dispatch (`openclaw`
  vs `codex`). Caller contract `infer(prompt, *, json_mode, timeout,
  model) -> InferResult` stays stable across backends.
- `codex` installed on the VPS via `~/.codex/auth.json` SCP from a
  laptop browser-OAuth flow.

### Phase 2 — three-tier shared library + brain

- `agents/shared/telegram.py` — unified send + 429 backoff. Replaces
  five duplicated reimplementations.
- `agents/shared/google_oauth.py` — wraps `InstalledAppFlow` for
  family-calendar, meetings-coach, connector.
- `agents/shared/heartbeat_base.py` — `HeartbeatProbe` base class
  consolidating six per-agent heartbeat scripts.
- `agents/shared/playwright_profile.py` — persistent profile launcher
  for stock-Chromium consumers (LinkedIn, WhatsApp).
- `agents/shared/camoufox_proxy.py` + `retry_policy.py` — hardened
  Camoufox + sticky residential proxy + reauth retry classifier.
  Consumed by Costco and Amazon flows.
- `agents/shared/brain.py` — codifies the read/write boundary on the
  `~/Dropbox/openclaw-backup/` brain (git-tracked config in
  `ops/brain/*`, runtime state in Dropbox-only paths).
- Each new module ships with an exemplar agent migration that proves
  the abstraction.

### Phase 3 — pilot liberation: Lowly Worm (news-digest)

- `news-digest/scripts/fetch-and-rank.py` and
  `update-preferences.py` flipped from `subprocess.run(["openclaw",
  "infer", ...])` to `from agents.shared.llm import infer`. Drops
  ~50 lines of subprocess plumbing per file.
- New `ops/scripts/llm-cron-runner.py` loads SOUL.md + IDENTITY.md +
  per-cron prompt, calls `llm.infer()`, pipes through
  `agents.shared.telegram` delivery.
- News-digest's morning crons moved to plain system crontab via
  `ops/scripts/install-host-cron.sh` host wrappers, off the OpenClaw
  LLM cron runtime entirely.

### Phase 4 — fleet port (5 remaining agents)

- family-calendar, meetings-coach, shopping, connector, and fix-it
  all migrated onto the shared library and host-cron runtime.
- Per-cron prompts extracted to `agents/<agent>/prompts/<name>.md`.
- Old OpenClaw crons disabled one at a time, validated against
  `fleet-health.json`.
- `fix-it/scripts/cron-self-check.py` rewritten to diff
  `expected-crons.json` against `crontab -l` instead of
  `openclaw cron list`.
- Five-AM-PT (12:00 UTC) fleet-delivery path: every morning gather
  cron writes `cache/morning-brief-ready.txt`, and a single
  `morning-fleet-deliver-host.sh` wrapper delivers all five agents'
  briefs at 12:00 UTC sharp via a `--hold-until` barrier.

### Phase 5 — deploy.py safeguards rewrite

- Safeguard 6 (smoke test): `oc("cron", "run", ...)` →
  `llm-cron-runner.py --dry-run`.
- Safeguard 7 (manifest validation): `oc("config", "validate")` →
  pure-Python `validate_manifest()`.
- Safeguard 8 (exec-approvals baseline): retired with tombstone.
  The OpenClaw approvals concept no longer exists.
- New regression test `test_deploy_safeguards.py` monkeypatches `oc`
  to raise on any call and asserts dry-run still passes.

### Phase 6 — decommission OpenClaw processes

- All OpenClaw-registered crons removed via final `openclaw cron rm`
  sweep.
- Gateway container stopped (image retained).
- `CLAWFORD_LLM_BACKEND=codex` promoted to default in
  `agents/shared/llm.py`.

### Phase 6.5 — host-native script execution

- All `ops/scripts/*-host.sh` wrappers rewritten to invoke
  `/usr/bin/python3` directly against bind-mounted scripts. No more
  `docker exec` into the gateway container.
- New regression guard `test_host_cron_wrappers.py` enforces no
  `docker exec` in non-comment wrapper code and explicit
  `/usr/bin/python3` everywhere.
- `ops/scripts/install-host-deps.sh` installs every Python package
  the fleet needs on the host (60+ packages: playwright, camoufox,
  feedparser, linkedin-api, amazon-orders, curl_cffi, etc.). New
  companion `install-host-system-deps.sh` for apt prerequisites.
- `ops/scripts/fleet-health.py` invocation path moved off
  `docker exec` onto bare host subprocess.
- `agents/fix-it/scripts/security-audit.py` rewritten as native
  Python (no more shelling to `openclaw security audit`).

### Phase 7a — deploy.py helper sweep

- Deleted 15 OpenClaw-coupled symbols from `deploy.py`: `oc`,
  `oc_json`, `oc_cron_*`, `fetch_live_crons`, `plan_cron_ops`,
  `apply_cron_ops`, `ensure_channel`, `ensure_binding`,
  `ensure_approvals`, `check_compose_yml_drift`, `GATEWAY_CONTAINER`,
  `COMPOSE_RUNTIME_PATH`, `COMPOSE_TRACKED_PATH`. ~350 LoC removed.
- `agents/shared/deploy_wrapper.sh` deleted.
- `ops/docker-compose.yml` and `ops/exec-approvals-baseline.json`
  deleted from git (Safeguard 11 retired).
- Structural test `test_phase7_openclaw_helpers_deleted` enforces
  the deletions stay deleted.

### Phase 7b — `~/.openclaw/` → `~/.clawford/` rename

- Workspace root renamed: every reference under `agents/` and
  `ops/scripts/` swept from `~/.openclaw/` to `~/.clawford/`. Six
  per-area commits, ~180 path strings, ~125 tracked files.
- Env file moved: `~/openclaw/.env` → `~/clawford/.env`. Three callers
  updated (`deploy.py:VPS_ENV_FILE`, all 7 `*-host.sh` wrappers, both
  `set-bot-*.sh` scripts).
- Env vars renamed: `OPENCLAW_REPO_ROOT` → `CLAWFORD_REPO_ROOT`,
  `OPENCLAW_WORKSPACE_BASE` → `CLAWFORD_WORKSPACE_BASE`.
- New regression guard `test_phase7b_openclaw_paths_retired.py` walks
  every file under `agents/` and `ops/scripts/` and fails on any
  reference to a forbidden openclaw path/env-var pattern. The
  Dropbox brain root `openclaw-backup` is preserved (renaming would
  reset Dropbox sync history).
- VPS physical rename: `mv ~/.openclaw ~/.clawford` (3.9G atomic),
  `cp ~/openclaw/.env ~/clawford/.env`. Legacy `~/openclaw/`
  directory left intact as a graveyard for separate cleanup.
- `install-host-cron.sh` re-run: 24 stale CONTRACT entries evicted
  and rewritten with `/home/openclaw/.clawford/` paths.
- `deploy.py --all` re-run: every workspace refreshed against the
  new repo source. 6 backup tarballs + 6 Dropbox mirrors.
- Vestigial `Manifest.approvals_policy` / `approvals_security` /
  `approvals_allowlist` dataclass fields deleted.
- Fleet-health verified 6/6 ok post-rename.

### Phase 7c — root-level docs + version bump

- `README.md` rewritten with Clawford-native framing. Points at
  `guide-v3/` as the live guide; flags `guide-v2/` as frozen
  historical.
- `VERSION` bumped 0.3.0 → 1.0.0.
- This CHANGELOG entry.
- `AGENTS-PATTERN.md` and `DEPLOY.md` audited for OpenClaw scar
  tissue. Targeted edits to the canonical workflow + safeguards
  table; the historical Gotchas section in DEPLOY.md is kept as an
  archaeological record of the OpenClaw era and flagged as such.
- `guide-v2/index.md` carries a frozen-notice header pointing at
  `guide-v3/`.

### Phase 7e — host system deps install script

- New `ops/scripts/install-host-system-deps.sh` — sudo apt installer
  for libjpeg-dev / libfreetype6-dev / zlib1g-dev / libpng-dev (pillow
  build prereqs) plus xvfb / openbox (camoufox headful fallback).
  Documented as a prereq in `install-host-deps.sh`.

### Phase 7d — guide-v3 chapter migrations (multi-session)

In progress at the time of cut. The post-1.0.0 chapter migration
track continues through `docs/v2-to-v3-migration.md`.

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
- 10-chapter setup guide (`guide-v1/`, originally `guide/`)
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
