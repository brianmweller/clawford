# Infra setup

*Last updated: 2026-04-17 · Reading time: ~20 min · Difficulty: moderate*

**TL;DR**

- A Clawford fleet runs on three boring pieces. A **shared library** under `agents/shared/*` that handles everything the agents need from the outside world. A **shared brain** under `ops/brain/*` (git) and `~/Dropbox/clawford-backup/*` (Dropbox) that gives the fleet durable cross-agent state. A **host-cron runtime** that fires plain Python scripts from `crontab -l` on a schedule. Everything else is implementation detail.
- The shared library is organised by how hostile the world you're talking to is. Tier 1 is clean APIs (Telegram, Google OAuth). Tier 2 is stock Playwright behind a browser profile. Tier 3 is hardened Camoufox behind a residential proxy. Every consumer that needs a scraper, a login flow, or a notification pipe goes through one of these three modules — agents don't reinvent any of it.
- The deploy tool — `agents/shared/deploy.py` — reads each agent's `manifest.json` and runs ten active safeguards before touching the VPS. Three of them exist because of specific past outages. Two earlier ones (Safeguards 8 and 11) have been retired because the platform quirks they guarded against no longer exist; Safeguard 12 (pip-audit) was added in the 2026-04-16 P1 hardening pass.
- Dropbox on a headless VPS is still the fiddliest thing in the setup. Budget an hour the first time. Apply exclusions within sixty seconds of linking the daemon or it will download your entire Dropbox account.

## The shape of the runtime

Before the libraries, before the brain, before the safeguards — the thing to hold in your head is the call graph. A morning briefing from one of the agents looks like this:

1. At `30 10 * * *` UTC, the host crontab fires a line pointing at a Python script in the agent's workspace.
2. The script imports from `agents.shared.*` — whichever tier it needs — and does its deterministic work.
3. If it needs reasoning (summarising a long email, ranking news, parsing a messy transcript), it calls `agents.shared.llm.infer()`, which routes the call through a CLI that rides a ChatGPT Plus subscription at zero marginal cost.
4. If it needs a durable fact ("Sam said they're travelling next week"), it writes to the shared brain via `agents.shared.brain.write_brain()`.
5. If it needs to talk to the human, it writes a plain-text cache file at `<workspace>/cache/morning-brief-ready.txt`.
6. At `0 12 * * *` UTC, a second host-cron line aggregates every agent's cache file into a single morning brief and sends it via `agents.shared.telegram.send_telegram()`.

Nothing in that path depends on a gateway container, a skill runtime, an exec-approvals allowlist, a policy file, or a platform API. It is six boring pieces, composed. That is the point.

## The shared library

`agents/shared/*` is the world-access layer. Everything an agent needs to touch anything outside its own workspace goes through one of these modules. The three tiers are named for the *kind of hostility* the target presents, not the tech stack.

### Tier 1 — clean APIs

The parts of the outside world that publish real APIs, issue real tokens, and return real JSON. You're writing the equivalent of an HTTP client library, plus error handling, plus one layer of convenience for the 80% case.

- **`agents/shared/telegram.py`** — `send_telegram(token, chat_id, text, *, silent, reply_markup)` plus `timed_send()` for the morning-brief fleet path. Unified 429 backoff. One HTTP client, one set of tests, six consumers.
- **`agents/shared/google_oauth.py`** — `build_flow`, `get_credentials`, `refresh_if_stale`. Wraps the upstream `InstalledAppFlow` with the detail you always forget: token refresh, scope validation, the fact that Desktop credentials need a redirect URI of `http://localhost`. The pattern for fresh auth is always: run it on a laptop, SCP the token to the VPS.
- **`agents/shared/heartbeat_base.py`** — `HeartbeatProbe` base class. Every agent ships a `scripts/heartbeat.py` that subclasses this and emits a JSON status line on stdout. The base class handles the timing envelope, the error catch, the exit-code discipline. Subclasses implement `probe() -> dict` and that's it.
- **`agents/shared/llm.py`** — `infer(prompt, *, json_mode, timeout, backend) -> InferResult`. The LLM broker. Routes calls through `codex infer` by default. The backend dispatch is abstracted so a future second provider slots in behind the same interface.
- **`agents/shared/brain.py`** — see the shared-brain section below.

### Tier 2 — stock Playwright

The parts of the outside world that don't publish an API, but don't actively fight you either. A persistent browser profile with ordinary Chromium, running under Xvfb on the VPS so the login flow sticks across sessions. Cookies live. Tokens refresh on their own.

- **`agents/shared/playwright_profile.py`** — `launch_persistent_profile(profile_dir, *, headless, xvfb_display)`, `ensure_xvfb(display_num)`, `cleanup_profile_lock(profile_dir)`. The mechanics of "launch Chromium in a way that preserves the logged-in session across reboots." Consumers today: the LinkedIn keepalive for one of the agents, the Google Messages Web scraper for another.

The Tier 2 story is boring and that's fine. The hostile world starts one tier up.

### Tier 3 — hardened Camoufox behind a residential proxy

The parts of the outside world that fingerprint you, detect headless browsers, present interactive Azure B2C login flows, and aggressively rotate their anti-bot rules. Think Costco. Think any retail site with fraud scoring. Stock Playwright fails in this layer — not because it *can't* log in, but because it gets flagged two sessions later and starts serving interstitials.

- **`agents/shared/camoufox_proxy.py`** — `get_proxy_config(env_var, *, prefer_sticky)`, `launch_camoufox(proxy_cfg, *, width, height)`, bare-IP fallback helpers. Parses the residential proxy URL from env, detects sticky vs rotating ports, hands a ready-to-use context to the agent script.
- **`agents/shared/retry_policy.py`** — `classify_selfasserted_response`, `should_retry`, exponential backoff with the specific tuning the one shopping agent's login flow needs. Promoted out of a single agent's `reauth_retry_policy.py` when a second Tier 3 consumer came along.

Tier 3 is the most intricate and the most fragile, and it's also the tier that most benefits from living behind one canonical module. Every time the upstream login flow changes, there's exactly one place to fix it.

### Why three tiers

The tier line isn't about tech stack — it's about *how much the remote side wants you to automate them*. A clean API (Tier 1) wants you there and ships the plumbing. A normal web app (Tier 2) doesn't care. A hostile retailer (Tier 3) actively fights you and updates its defenses weekly.

Knowing which tier a new integration belongs in before writing a line of code is half the battle. Every time a new integration gets written in the wrong tier, the wrong one breaks first and the fix is "move it to the right tier." Better to land it in the right tier from the start.

## The shared brain

The shared brain is a first-class subsystem. It's the difference between a fleet of agents and a pile of scripts.

The short version: it's a directory of plain markdown files with a small structured schema on top, split into a **git-tracked half** (`ops/brain/*`, for canonical config and schemas) and a **Dropbox-synced half** (`~/Dropbox/clawford-backup/*`, for runtime state). Every agent reads and writes through `agents/shared/brain.py`, which enforces the split structurally and refuses writes to the git-tracked side. All writes are appends.

The full chapter is [Ch 16 — The shared brain](16-shared-brain.md): why the brain exists, the two halves and how they sync, the four primitives (facts, commitments, tasks, notes), the append-only rule, and the pitfalls. Read it before writing an agent that writes to the brain.

## The host-cron runtime

Every scheduled job in the fleet fires from the host crontab. That is the entirety of the scheduling story.

There's a script, `ops/scripts/install-host-cron.sh`, that owns the contents of `crontab -l`. The script contains a list of `CONTRACT_ENTRY` lines — one per scheduled job — and it reconciles the live crontab against that list. It's drift-aware: if a schedule, path, or timeout changes between runs, the stale line is evicted and the new one installed. No manual `crontab -e`. No drift between what's in git and what's actually scheduled.

Each agent declares its own crons as `CONTRACT_ENTRY` lines in the install script. The script writes them under a stable marker comment, so other tools (the heartbeat check, the fix-it agent's cron-self-check) can parse them back out and verify nothing has disappeared.

> **Wrapper exec bits live in git, not at install time.** The wrappers under `ops/scripts/*-host.sh` must be committed as mode `100755`, not `100644`. If one lands at `100644`, `install-host-cron.sh` will `chmod +x` it on every run, which produces a recurring dirty working tree on the VPS (`old mode 100644 / new mode 100755` forever, refusing every subsequent `git pull`). Flip existing files with `git update-index --chmod=+x <path>` — the change touches zero bytes of content, has no line-ending implications, and fixes the recurrence permanently. More generally: anything an installer does repeatedly at runtime that a single commit could encode, bake into the commit. That's the difference between idempotent-in-principle and idempotent-in-practice.

### The 5 AM PT fleet path

Every agent that contributes to the morning briefing follows the same shape:

1. Populate at `30 10 * * *` UTC (3:30 AM PT, ~1.5h of slack).
2. Write plain text atomically to `<workspace>/cache/morning-brief-ready.txt`.
3. Do **not** call `telegram.send_telegram()` directly from the morning orchestrator.
4. The fleet aggregator at `0 12 * * *` UTC (5:00 AM PT) reads every agent's cache file and sends a single consolidated brief.

The fleet-path discipline matters because without it, six agents each send their own early-morning message at six slightly different times and the human wakes up to a notification storm instead of one actionable digest. The 3:30 AM populate / 5:00 AM deliver split gives every agent an hour and a half to be late without breaking the brief, and the atomic-write-to-cache pattern means a partially-failed agent cleanly drops out of the brief instead of corrupting it.

### Retiring an agent — the file-based opt-out pattern

`install-host-cron.sh` reads `$HOME/.clawford/disabled-agents.txt` on every run. If an agent id appears in that file, the installer skips its `CONTRACT_ENTRY` lines on fresh installs *and* evicts any matching lines already in the live crontab. The file format is one entry per line, with `#` comments and blank lines ignored; missing file means no agents are disabled. Retiring an agent is two operator-facing steps — append the id to the file, re-run the installer — and reversal is one step: remove the line, re-run.

This is the canonical file-based opt-out pattern, and the rule generalises. When you need a persistent, reversible operator opt-out — "disable this check", "skip this cron", "turn this agent off" — use a single text file under `$HOME/.clawford/` that the installer reads on every run. Avoid in-code flags (they need a redeploy to toggle), environment variables (they don't survive cron invocations), and manifest fields (they need a deploy cycle). A text file the operator can `cat`, `echo >>`, or `$EDITOR` is auditable in seconds, scriptable from emergency flows, and survives git pulls and reboots. The path should be overridable via an env var (the `DISABLED_AGENTS_FILE` pattern) and the installer should wire any operator-facing helper — `retire.sh`, say — to *append* to the file, never overwrite, so multiple disabled things can coexist.

**Operator discipline caveat: spell out full agent ids.** The matcher inside `install-host-cron.sh` is prefix-with-hyphen-boundary, not exact. Writing `fix-it` into `disabled-agents.txt` disables `fix-it`, `fix-it-brain-validation`, `fix-it-conflict-scan`, and every other `fix-it-*` cron — which is exactly what retirement wants. But writing `fix` would silently nuke every `fix-it-*` cron too, because `fix-it-*` starts with `fix-`. The installer cannot tell `fix` from `fix-it`; it has no canonical agent list to disambiguate against. Always type the full canonical id: `fix-it`, not `fix`. The rule generalises — anywhere in the system an agent id gets matched against a string (cron markers, filter rules, deny-lists, grep patterns), type the whole thing. A typo at one character of prefix is a disable-the-whole-family outage waiting to happen.

### Logic-gate the LLM

The default is deterministic Python. `agents.shared.llm.infer()` exists for the cases where the input is genuinely free-form natural language that keyword matching can't handle reliably.

Pure Python wins for: structured calendar events, structured order records, structured invite fields, templated output. LLM wins for: raw newsletter bodies, raw family chat transcripts, the content of a meeting debrief. If a cron looks like "given structured data, produce a templated message," it doesn't need the LLM and it shouldn't call the LLM.

This isn't a philosophical stance. It's a cost-and-failure discipline: the LLM calls are the parts of the fleet that produce weird outputs on bad days. Keeping the deterministic paths deterministic shrinks the surface area where weird outputs can happen.

## `deploy.py` and its safeguards

`deploy.py` reads an agent's `manifest.json` and installs the agent onto the VPS idempotently: copies the workspace files into `~/.clawford/<agent>-workspace/`, seeds any declared state files, syncs `agents/shared/` into the workspace, and captures a pre-deploy backup tarball. It is one Python script, ~1800 lines, and it is the single structural choke-point between the git repo and the running fleet.

It runs ten active safeguards (two earlier safeguards, 8 and 11, were retired during the liberation; Safeguard 12 was added during the 2026-04-16 P1 hardening pass — all three are covered below). Two of the ten are worth naming explicitly, because they exist because of specific past outages.

### Safeguard 9: forbidden cron-message patterns

Safeguard 9 statically walks every manifest's `crons[].message` field and refuses to deploy if any of them contain a pattern from a blocklist. The blocklist has two distinct classes and the stories behind them are both worth remembering.

**Class 1 — shell operators.** The original outage is the opening scene of [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md). A cron message said "run `python3 foo.py; printf 'EXIT:%s' $?` and capture the exit code." An upstream preflight rejected any interpreter invocation combined with shell operators like `;`, `&&`, output redirects, `sh -lc`, or exit-code capture. The rejection cascaded into "approval required" alerts across the whole fleet overnight. The fix was the script contract: scripts print one JSON line with a `status` field on stdout, and cron messages invoke them bare. Safeguard 9 grep-matches every shell operator in the blocklist and refuses the deploy.

**Class 2 — legacy prose clauses.** An earlier version of every agent's cron prompts ended with some variant of "Update your status file." Post-migration, per-agent `*.status.md` files are legacy — `fleet-health.json` is the authoritative health surface and nothing reads the status files anymore. Keeping the prose in the prompts drifted agent LLMs into writing status files with whatever schema and header they invented on the day, which is how one agent's status file got committed with a subtly-wrong header and tripped `brain-validation`. The fix had three layers: retire the validator's header check, strip the "Update your status file" clause from every cron prompt in the fleet, and add that exact string to Safeguard 9's pattern list as a regression guard.

The full blocklist lives in `agents/shared/deploy.py` at `CRON_MESSAGE_FORBIDDEN_PATTERNS`. Adding a new pattern is a perfect red/green-refactor cycle: write the failing test case first in `agents/shared/tests/test_cron_message_hygiene.py`, watch it go red, add the pattern, watch it go green. It's the cheapest place in the repo to prevent a class of outage.

### Safeguard 10: config-source resolution

Real agent config files — `SOUL.md`, `IDENTITY.md`, `TOOLS.md`, and friends — are **gitignored**. Only the `*.example` templates are tracked. This is a deliberate consequence of a PII sanitization pass: the real files contain family names, DOBs, calendar IDs and similar; the templates contain a generic `Sam / Alex / Jamie / Avery / Jordan` cast.

On a fresh clone, there are templates but no real files. Deploying in that state is a bug. Safeguard 10 walks every `config_files[]` entry in the manifest and classifies each one:

- **Real file present, no sentinel** → proceed.
- **Real file missing, `.example` sibling present** → exit 5 with a `--bootstrap-configs` hint.
- **Real file missing, no `.example` sibling** → exit 5 "truly broken manifest."
- **Real file present but still carries the `CLAWFORD_BOOTSTRAP_UNEDITED` sentinel on its first line** → exit 5 naming the files.

`deploy.py <agent> --bootstrap-configs` scaffolds each missing real file by copying its `.example` sibling and prepending the sentinel comment. The operator then hand-edits the dummy values to real ones, deletes the sentinel line, and redeploys. The sentinel is the rail that stops someone from accidentally shipping the `Sam / Alex` cast into a live agent's workspace after a fresh clone.

### The two retired safeguards

Earlier versions of the deploy tool shipped two safeguards that are now gone. Both existed for good reasons, both stopped classes of regressions from happening twice, and both were retired because the thing they guarded against ceased to exist during the liberation.

**Safeguard 8 — exec-approvals baseline drift.** Checked the live `exec-approvals.json` against a committed baseline file (`ops/exec-approvals-baseline.json`) and refused to deploy if the two diverged. It existed because a platform upgrade had silently rewritten the live approvals file in a way that blocked every cron across the fleet overnight. The liberation retired both the platform and the approvals concept; Safeguard 8 and its baseline file are gone. Its call site in `deploy.py` is a one-line tombstone comment with the removal date. The historical story is in [Ch 02](02-what-isnt-clawford.md).

**Safeguard 11 — docker-compose drift.** Compared the runtime `~/openclaw/docker-compose.yml` to the git-tracked `ops/docker-compose.yml` and refused to deploy if the two diverged, because a bind-mount edit had once lived on the VPS for hours before landing in the repo. The cleanup deleted the compose file from git (the gateway container was already stopped earlier) and Safeguard 11 had nothing left to check. Retired with a tombstone comment on the same day as Safeguard 8.

### The other safeguards

- **Safeguard 1 — Backup.** Pre-deploy tarball, rotated to 10 per agent. Non-skippable. Recovery is `tar -xzf`.
- **Safeguard 2 — Source clean gate.** Refuses to deploy with uncommitted edits or untracked files in the agent directory. Overrideable with `--allow-dirty` for specific emergencies.
- **Safeguard 3 — Diff preview + confirm.** Shows unified diffs for every config file the deploy would overwrite and prompts for confirmation. Skippable per-file with `--yes-updates`. Chattr-aware — handles immutable files correctly.
- **Safeguard 4 — Drift detection.** Compares SHA256 hashes of manifest-tracked files against the last-deploy baseline. Refuses to deploy if the workspace has changed out from under the tool. Overrideable with `--accept-drift` (violation is logged). Enforces the no-on-VPS-dev rule structurally.
- **Safeguard 5 — Banner.** Prints the workflow contract before every deploy: local git → VPS workspace, edits on the VPS will be overwritten unless committed back to local git first. The banner is the nudge that keeps future-you from accidentally starting to live-edit the running VPS.
- **Safeguard 6 — Post-deploy smoke test.** If the manifest declares a `smoke_test` and `--smoke-test` is passed, runs the declared script (defaults to `scripts/heartbeat.py`) as a host subprocess and asserts exit 0 + non-empty stdout. On failure, auto-restores the pre-deploy backup. The subprocess transport is a Phase-5 rewrite; earlier versions fired an LLM cron and polled a gateway API, which the liberation moved off of.
- **Safeguard 7 — Manifest validation.** Cross-field structural check on the parsed `Manifest` dataclass: every config file includes `SOUL.md` and `IDENTITY.md`, the scripts list contains `scripts/heartbeat.py`, no duplicate cron names, `smoke_test.script` references a real entry in the scripts list, state-file paths are relative. Catches real developer errors before the tool touches the VPS.

All safeguards run in an order that puts the file-system-touching ones *after* the read-only ones, so a failure at any structural check rolls back cleanly with nothing on disk.

## How tests work here

Infrastructure code in this repo — the deploy tool, the host-cron installer, the fleet-health orchestrator, the cookie managers, anything that mutates VPS state — is built test-first. Structural tests (grep the script for a forbidden substring) are cheap and useful and have their place, but they do not substitute for running the real thing against a stubbed environment and watching the real observable behavior.

### The harness pattern

Every infra-layer test that matters follows the same shape: **subprocess the real script against a stubbed environment, then inspect the state files it touched.** Concretely:

- **Stub the VPS-shaped commands.** For the cron installer, that means stubbing `crontab` with a shell shim that reads + writes a state file (`$STATE`). For the deploy tool, that means stubbing `ssh` / `scp` / `rsync`. Every stub lives under `agents/shared/tests/stubs/` and is installed on `$PATH` via a pytest fixture that prepends the stubs directory.
- **Stub `$HOME` and the env vars.** `monkeypatch.setenv("HOME", str(tmp_path))` and set every env var the script reads to a known value. The test runs against `tmp_path`, never against the real filesystem.
- **Subprocess the real script.** `subprocess.run([str(script_path), ...], env=stubbed_env, capture_output=True, text=True)`. No monkey-patched internals, no direct function calls — the test exercises the same code path a real operator invocation would.
- **Inspect the state files the script touched.** The assertions read `$STATE` (the stubbed crontab state), the stubbed `~/.clawford/` tree, the stubbed `.env`, and compare against the expected post-condition. The assertion is "after running this, the crontab state file contains exactly these lines," not "after running this, the code returned this value."

The `agents/shared/tests/conftest.py` file ships the fixtures that make this pattern cheap: `stubs_on_path`, `fake_home`, `fake_crontab`, and a handful of assertion helpers. The next infra test should start by importing those fixtures, not by reinventing them.

### Pitfalls inside the harness itself

The harness is infra code too, and the same rule applies to it — the stubs are not exempt from TDD. Two harness bugs have bitten the fleet on 2026-04-15 and are worth calling out.

**Pitfall 1 — Skipped tests drift silently.** An earlier version of `test_install_host_cron.py` had a `@pytest.mark.skipif(sys.platform == "win32")` on it and used seed crontab lines that referenced a pre-Phase-6.5 container path (`/home/node/.openclaw/...`). The skipif meant the test never ran on Windows dev boxes, and the post-Phase-6.5 rename to `/home/openclaw/.openclaw/...` never landed in the seed lines either, because nobody ever noticed the test was broken — it was invisibly skipped. The fix was to catch this during a fresh TDD run against the real installer on the VPS, where the stale seed lines produced a visible mismatch. **Rule:** any time you touch an infra file, run its *neighbors'* tests too. Pre-existing bugs hide behind skipifs and stale comments.

**Pitfall 2 — Test stubs for piped commands need atomic write, not bare redirect.** The canonical case is `crontab`, which has `crontab -l` for reads and `crontab -` for writes, and is commonly used by shell code in a read-then-write pipeline against itself:

```bash
{ crontab -l; for l in "${NEW_LINES[@]}"; do echo "$l"; done } | crontab -
```

A naive stub writes the new content with a bare redirect:

```bash
#!/usr/bin/env bash
# buggy stub
if [ "$1" = "-l" ]; then cat "$STATE"; fi
if [ "$1" = "-" ];  then cat > "$STATE"; fi   # ← truncates $STATE at pipeline setup time
```

The bug is subtle. `cat > "$STATE"` opens `$STATE` for writing at pipeline setup time, **before** the left-hand `crontab -l` has a chance to drain the existing content. The reader sees an empty file, the writer writes `{empty + NEW_LINES}` back, and every pre-existing line silently vanishes. Worse, an eviction test that seeds the state, runs the installer, and asserts the seed is gone will **pass for the wrong reason** — the race deleted the seed, not the installer's eviction logic. False green on a test that was supposed to verify a safety check.

The fix is a one-line change to the stub — buffer stdin into a temp file and atomically rename:

```bash
#!/usr/bin/env bash
# correct stub
if [ "$1" = "-l" ]; then cat "$STATE"; fi
if [ "$1" = "-" ];  then tmp=$(mktemp); cat > "$tmp"; mv "$tmp" "$STATE"; fi
```

The rename defers the state overwrite until stdin has been fully drained, and the race closes. **Rule:** any stub for a command that might be piped through itself in a read-then-write construction must use atomic replacement on the writer side. A cheap smoke test: `printf 'a\nb\n' > state; { crontab -l; echo c; } | crontab -; grep -q '^a$' state` — if that fails, the stub has the bug. Both crontab stubs in the test suite now use atomic replacement, and the `agents/shared/tests/stubs/crontab` shim is the canonical implementation.

### When to skip TDD

Almost never, for infra code. The exceptions are shallow enough to enumerate:

- **Pure prose changes.** Editing a comment, a docstring, or a `.md` file doesn't need a test. Run the grep voice-grips, not pytest.
- **One-shot migration scripts that run exactly once and then get deleted.** Even then, a dry-run preview with a fake target is usually cheaper than debugging a misfire after the fact.
- **Config-only changes** where the behavior under test is "does the tool read the new key out of the config correctly" — if the existing tests cover the config-loading path, the new key is covered transitively.

Everything else — any code that writes a file, mutates the crontab, touches `~/.clawford/`, talks to Dropbox, or invokes `ssh`/`scp`/`rsync` — gets a test first, always.

## Dropbox on a headless VPS

Dropbox on a headless Linux VPS is the single fiddliest thing in the setup. Not because it's hard — because the fail modes are silent and the defaults assume a desktop user. Budget an hour the first time.

The install is straightforward:

```bash
cd ~ && curl -Ls 'https://www.dropbox.com/download?plat=lnx.x86_64' | tar xzf -
~/.dropbox-dist/dropboxd
```

The first run prints an authorization URL. Open it in a browser, authorize, and the daemon confirms the link. Ctrl-C out of the foreground run, then restart under systemd so it survives SSH disconnects.

Install the `dropbox` CLI for status checks (`sudo apt install nautilus-dropbox`, then `dropbox status`).

### Selective sync is non-optional

If the Dropbox account has anything besides the brain folder — and it does — exclude everything else *before* the daemon starts pulling it down. Without exclusions, the VPS downloads the entire account and fills the disk. Apply exclusions *immediately* after linking, not "once you notice disk getting tight."

```bash
mkdir -p ~/Dropbox/Archive ~/Dropbox/Personal ~/Dropbox/Projects
dropbox exclude add ~/Dropbox/Archive ~/Dropbox/Personal ~/Dropbox/Projects
dropbox exclude list
```

> **Dropbox exclusions require full paths.** `dropbox exclude add Archive` silently does nothing — no error, just does nothing. Always use `~/Dropbox/Archive`. And if the daemon is ever unlinked and re-linked, every exclusion has to be re-applied immediately, because a re-link starts with none.

Only the brain folder (`clawford-backup/` or, for installs predating the rename, `openclaw-backup/`) should remain syncing.

### Known fail modes

- **Ghost folders.** Dropbox reports "Up to date" but a file isn't syncing. `dropbox filestatus <path>` shows `unwatched`. This happens after a re-link or when the folder existed in the cloud before the daemon was set up. Fix: delete the empty cloud-side folder and let the daemon upload the VPS's copy as new.
- **Daemon stops silently after SSH disconnect.** The daemon doesn't produce an error — it just stops. Fix: run under systemd instead of `nohup`, and watch `dropbox status` from the fleet-health probe.
- **Conflict files.** Simultaneous writes produce files like `active (conflicted copy 2026-04-08).md`. The fix-it agent monitors for these every two hours and alerts on Telegram. **Do not auto-merge conflict files.** Let the alert fire and resolve by hand. Auto-merging is how the commitment log gets corrupted.

## Telegram bots

Every agent gets its own Telegram bot. If they all share one, every message comes from the same sender, and at 3 AM when something alerts, it's not instantly obvious which agent is reporting. Separate bots give each agent its own name, its own avatar, and its own chat thread.

For each agent: open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts. Save the bot token straight into a local `.env` file as `{AGENT}_BOT_TOKEN=...` — never into git, never into a checked-in config. Optional: use `/setuserpic` to upload an avatar.

On the VPS side, the bot tokens live in `~/clawford/.env` (or, for installs predating the rename, `~/openclaw/.env`) and are loaded by `deploy.py` and by every `send_telegram` call in the fleet. The `.env` file is never committed.

Bot commands and descriptions get set via two small scripts:

- `scripts/set-bot-commands.sh` — sets each agent's custom `/` picker entries.
- `scripts/set-bot-descriptions.sh` — sets each agent's short and long descriptions.

Both scripts are idempotent and UTF-8 safe. Run either from the host any time.

> **Telegram aggressively client-caches bot surfaces.** After running either script, close and reopen the bot's chat on the phone. Pull-to-refresh doesn't always cut it, and the easy assumption is that the script didn't work when the real issue is a stale cache.

## Backups — what gets backed up and what doesn't

Backups in Clawford come in three tiers:

1. **Pre-deploy workspace tarballs.** `deploy.py` writes `~/.clawford/deploy-backups/<agent>-<timestamp>.tar.gz` before every deploy. Also mirrors the tarball to the Dropbox half of the brain so it survives a VPS loss. Recovery from a bad deploy is `tar -xzf`.
2. **Dropbox itself.** The entire brain runtime directory is continuously synced off-VPS by the Dropbox daemon. Losing the VPS means re-provisioning and re-cloning — the brain state is in Dropbox.
3. **Monthly brain archival.** The fix-it agent archives completed tasks and stale facts older than 90 days into `<brain>/archive/YYYY-MM/` on a monthly cron. Archive is permanent — nothing in `archive/` is ever deleted — so the whole history of the brain is recoverable.

What's *outside* the Clawford backup system:

- **Secrets.** `.env` files, bot tokens, API keys. Gitignored and Dropbox-ignored by design — they never live in git or Dropbox. They live in a password manager.
- **Terraform state.** Hetzner's state file is local to wherever `terraform apply` was run from. Remote state backend if you have strong feelings about it; local is fine otherwise.
- **The Docker image cache on the VPS.** A rebuild pulls fresh. Nothing irreplaceable lives there.

## Pitfalls you'll hit

> **Skipping selective sync.** Letting the Dropbox daemon pull down the entire account fills the VPS disk in an afternoon and leaves the daemon in a half-synced state. Apply exclusions within 60 seconds of linking. Every single top-level folder except the brain directory, full paths, no abbreviations. Re-apply after every re-link.

> **Editing commitment or task files directly from a markdown editor.** Agents append to these files with strict formatting conventions that their parsers depend on, and a casual human edit will silently break the parsing. Overdue detection will stop working; a resolved commitment will look open. Treat the brain as read-mostly from outside the fleet. If an entry needs to change, route it through an agent.

> **Auto-merging a Dropbox conflict file.** Dropbox creates `active (conflicted copy 2026-04-08).md` when two writers touch the same file simultaneously. Merging both versions automatically will eventually lose commitment state or double-write a fact — the "winning" version of a merge is not obvious when both copies have structured entries with different IDs. Let the alert fire, look at both files by hand, pick the one that's correct, delete the conflict copy.

> **Starting a new integration in the wrong tier.** Tier 1 code doesn't survive Tier 2 conditions, and Tier 2 code doesn't survive Tier 3 conditions. Every integration built in the wrong tier eventually gets rewritten. Before writing a line, decide whether the target is a clean API, a normal web app, or a hostile retailer — and use the tier that matches.

## See also

- [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md) — the decision doc that explains why the runtime looks like this and not like the platform it used to sit on top of.
