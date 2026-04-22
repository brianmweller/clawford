# Glossary

*Last updated: 2026-04-21 · Reading time: ~15 min · Difficulty: reference*

> **TL;DR.** Every Clawford-specific term that earns repeated use in the guide is defined here, alphabetically. Entries cross-link to the chapter that introduces the term properly. If you know the term you're looking for, Ctrl-F is faster than reading top-to-bottom. The last two sections collect **named incidents** (load-bearing war stories that are referenced by name across multiple chapters) and **retired terms** (things that used to matter in the pre-liberation fleet and are now historical).

## A–Z

**Agent.** One of the Python + prompt packages under `agents/{agent-id}/`. The public fleet ships five: [Mr Fixit](09-mr-fixit.md), [Lowly Worm](10-lowly-worm-newsfeed.md), [Mistress Mouse](12-mistress-mouse.md), [Sergeant Murphy](13-sergeant-murphy.md), and [Huckle Cat](14-huckle-cat.md). Each has its own workspace under `~/.clawford/{agent-id}-workspace/`, its own cron surface, and its own identity files.

**Brain (shared brain).** The fleet's cross-agent durable state, split across two storage backends: **`ops/brain/*`** in git (for state that should be versioned and code-reviewed) and **`~/Dropbox/clawford-backup/*`** on Dropbox (for state that should sync without versioning — people files, facts, commitments, inboxes). Access is via `agents.shared.brain.*`. See [Ch 06 — Infra setup](06-infra-setup.md).

**Busytown.** The Richard Scarry book series that provides the naming theme for the fleet. Each agent is a Busytown character (Mr Fixit the fox mechanic, Lowly Worm the worm, Mistress Mouse the mouse, Sergeant Murphy the pig detective, Huckle Cat the cat kid). Choosing names from a consistent fictional universe makes the agents memorable and distinguishable in logs, chats, and documentation.

**Callback shortcut.** A dispatcher fast-path that handles inline-button taps without invoking the LLM. When the operator taps a `[Confirm]` or `[Cancel]` button, the dispatcher parses the `callback_data` prefix (`confirm:`, `cancel:`, `confirm_all:`, `cancel_all:`, `like:`, `dislike:`, `more:`), routes directly to the appropriate executor or pending-action removal, and replies. The LLM is never called. See [Ch 18 — The inbox](18-the-inbox.md#2-the-dispatcher-dispatcherpy).

**Cache-is-not-a-queue rule.** The design rule that came out of [§ The 5x resend incident](13-sergeant-murphy.md#the-5x-resend-incident): delivery decisions are made from the current run's script output, never from iterating a cache directory. Staging files are a workbench for human confirmation, not a delivery queue. Applies to every cron-based staging-and-confirm flow in the fleet.

**Camoufox.** Hardened Firefox build with anti-fingerprinting patches. The Tier-3 browser in the shared library, used by [Huckle Cat](14-huckle-cat.md) for the Google Messages Web pairing flow. Wrapped by `agents/shared/camoufox_proxy.py`. See [Ch 17 Shape 5](17-auth-architectures.md#shape-5-camoufox-residential-proxy-auto-mfa) and [Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice).

**`chattr +i`.** Linux "immutable" filesystem attribute. Applied to `SOUL.md`, `IDENTITY.md`, `TOOLS.md`, and `AGENTS.md` in every agent workspace after every deploy. Blocks writes even by root, which is the whole point — an LLM code path that tries to rewrite its own identity file fails with `Operation not permitted`. See [Ch 19 § Defense layer 1](19-security-and-hardening.md#defense-layer-1-os-level-immutability).

**Clawford.** The fleet runtime. A personal single-operator AI agent system built on plain Python, host crontab, `codex` via ChatGPT Plus, Dropbox, and Telegram. No gateway container, no platform SDK, no custom CLI. See [Ch 01 — What is Clawford?](01-what-is-clawford.md) for the introduction and [Ch 02 — What Clawford isn't](02-what-isnt-clawford.md) for the manifesto.

**`codex infer`.** The CLI entry point for the `codex` tool that rides a ChatGPT Plus subscription. Every LLM call in the fleet routes through `agents.shared.llm.infer`, which calls `codex infer` under the hood. Zero marginal cost per call. See [Ch 04 — VPS setup](04-vps-setup.md).

**Commitment.** A durable, human-confirmed action item with a `by_when` date. Lives in `commitments/active.md` in the shared brain, tagged with the source agent. Written exclusively by agents after the operator `/confirm`-s the staged version via Telegram. Read by [Sergeant Murphy's commitment-follow-up cron](13-sergeant-murphy.md) and by the coaching side of the post-meeting scan.

**Confirm executor.** A tool executor that lives in `EXECUTORS` but **not** in `TOOLS` — meaning the LLM cannot call it directly. The only path to a confirm executor is the operator tapping a Confirm button on a staged pending action. Examples: `confirm_calendar_add` (Mistress Mouse), `confirm_add_note` (Huckle Cat). See [Ch 18 § The tools.py pattern](18-the-inbox.md#the-toolspy-pattern).

**Compose/deliver split.** The pattern where an expensive LLM composition runs in one cron (typically at `30 10 UTC`) and a cheap aggregation+delivery step runs in a later cron (typically at `0 12 UTC`), gated by a cache file (`cache/morning-brief-ready.txt`). Introduced to work around the retired 600-second LLM cron timeout; preserved post-liberation because it also maps cleanly onto the [5 AM PT fleet delivery path](#az).

**Contract.** Shorthand for [the script contract](#az).

**Conversation window.** Per-agent sliding window of the last 20 input items, persisted as JSONL at `~/.clawford/inbox/{agent-id}.jsonl`. A 1-hour inactivity timeout drops older items so stale context does not leak into a new session. Implemented by `agents/shared/conversation.py`. See [Ch 18 § Conversation window](18-the-inbox.md#4-the-conversation-window-conversationpy).

**`contract_wrap.py`.** The helper in `agents/shared/` that wraps every cron-invoked script to enforce [the script contract](#az). Every `main()` entry point in every agent script goes through `contract_wrap.contract_main(probe_fn)`.

**CRONS.md.** The per-agent authoritative spec for every host cron the agent registers. Lives at `agents/{agent}/CRONS.md`. The source of truth that `install-host-cron.sh` is validated against. Operator-editable.

**Dispatcher (`dispatcher.py`).** The stateless request handler for inbound Telegram messages. Routes each update to the right agent by bot-token resolution, applies the chat_id gate, handles callback shortcuts without invoking the LLM, and drives the tool-use loop for conversational queries. ~565 lines. See [Ch 18 § The dispatcher](18-the-inbox.md#2-the-dispatcher-dispatcherpy).

**`deploy.py`.** The single path code takes from the repo into a live agent workspace on the VPS. Runs 10 active safeguards before touching the VPS. Invoked as `python3 agents/shared/deploy.py <agent>` on the VPS, after a `git pull --ff-only`. See [Ch 07 — Intro to agents](07-intro-to-agents.md).

**Drift detection.** Safeguard 4 in `deploy.py`. Refuses to deploy if the workspace on the VPS has changed since the last deploy — i.e., if somebody edited a live workspace file by hand. Blocking, not advisory. See [Ch 19 § Defense layer 3](19-security-and-hardening.md#defense-layer-3-the-deploy-tool-safeguards).

**`.env`.** Per-agent workspace-local environment file. Gitignored. Contains bearer tokens, proxy credentials, TOTP secrets, Telegram bot tokens. Loaded by `agents.shared.env.load_env` on script startup. See [Ch 20 § .env schema](20-scripts-and-configs.md#env-schema).

**Fleet.** The collection of agents on a single VPS — typically the six Busytown agents in a default deployment. "The fleet" is the operator-facing unit; individual agents are deployable independently but they share the brain, the shared library, and the cron runtime.

**Fleet delivery path (5 AM PT).** The convention that every daily morning agent composes its output by `30 10 UTC` (3:30 AM PT) into a `cache/morning-brief-ready.txt` file, and a single `morning-fleet-deliver` cron aggregates all ready files at `0 12 UTC` (5:00 AM PT) and sends one composite Telegram message. The 30-minute slack is for composition runs that take longer than expected. See [Ch 06 — Infra setup](06-infra-setup.md).

**Fleet health.** The per-agent status reported by `ops/scripts/fleet-health.py`. Calls every agent's `heartbeat.py`, aggregates the results into `fleet-health.json` in the shared brain, and surfaces anomalies. The `fleet-health-host.sh` wrapper runs it every 15 minutes. A non-`ok` status from any agent is a blocker for any deploy.

**Heartbeat.** Every agent's `scripts/heartbeat.py` file, which subclasses `HeartbeatProbe` from `agents.shared.heartbeat_base` and implements a `probe() -> dict` method returning `ok` / `degraded` / `fail`. The sole writer of the agent's `HEARTBEAT.md` status file. Called by `fleet-health.py` at 15-minute cadence.

**`HEARTBEAT.md`.** Per-agent status file at the workspace root. Truncate-written by `heartbeat.py` on every probe. Contains the last probe timestamp, the `ok` / `degraded` / `fail` status, and a structured field for each sub-probe (auth, cron, cache-freshness, workflowy-session, etc.).

**Host cron.** A crontab entry on the VPS that fires a Python script directly, with no gateway container, no skill runtime, and no exec-approvals allowlist. The default runtime in a Clawford fleet post-liberation. Managed by `install-host-cron.sh`. See [Ch 06 — Infra setup](06-infra-setup.md).

**Inbox daemon (`telegram_inbox.py`).** A single async Python process that long-polls all six Telegram bots concurrently, routing each inbound message to the dispatcher. Per-agent offset persistence, per-update error isolation, kill-switch file for dev-vs-VPS toggle. Deployed as a systemd user unit (`clawford-inbox.service`). See [Ch 18 — The inbox](18-the-inbox.md).

**`IDENTITY.md`.** Operator-facing identity file in every agent workspace. Populated from `IDENTITY.md.example` during deploy and `chattr +i`-ed. Describes who the agent is to the operator (name, emoji, voice, bounded scope).

**Immutability.** See [`chattr +i`](#az).

**Kill-switch file.** `~/.clawford/inbox-disabled`. When this file exists, the inbox daemon exits cleanly at startup and the systemd unit's `ExecStartPre` check prevents restarts. Used to park the VPS daemon while the operator runs the inbox locally for development. See [Ch 18 § Deployment walkthrough](18-the-inbox.md#deployment-walkthrough).

**`install-host-cron.sh`.** The single source of truth for the VPS crontab. Drift-detects the current `crontab -l` against the `CONTRACT_ENTRIES` block embedded in the script, evicts drift, rewrites idempotently. Every cron in every agent lives in this file's CONTRACT_ENTRIES block.

**Liberation.** The project of migrating the fleet off the pre-Clawford gateway platform ("OpenClaw") onto a Clawford-native stack. Ran approximately 2026-04-02 through 2026-04-15, completing with the `.openclaw/` → `.clawford/` rename and the guide-v3 migration.

**Local-then-SCP.** The auth-flow idiom from [Ch 17 Idiom 1](17-auth-architectures.md#the-three-cross-cutting-idioms): any auth flow that needs a browser runs on the operator's local machine, produces a token file, and gets SCPed to the VPS after the fact. The VPS never runs a browser.

**Manifest (`manifest.json`).** Per-agent manifest at `agents/{agent}/manifest.json`. Declares `agent_id`, `workspace`, `config_files`, `crons`, and `smoke_test`. Consumed by `deploy.py` at Safeguard 7. See [Ch 20 § manifest.json schema](20-scripts-and-configs.md#manifestjson-schema).

**MCP (Model Context Protocol).** The protocol used by the meeting-transcript vendor integration in [Sergeant Murphy](13-sergeant-murphy.md#the-mcp-transcription-integration). Currently the only MCP consumer in a default fleet. Uses OAuth 2.1 for consent with the same local-then-SCP flow as Google OAuth.

**Mining pipeline.** The one-time, run-locally, human-review-gated seed for [Huckle Cat's](14-huckle-cat.md#the-mining-pipeline) people directory. Miners pull from Gmail, Google Calendar, Google Contacts, MCP transcription provider, Google Messages, and Workflowy; then aggregator + LLM enrichment + review markdown + `--finalize`. Step 0 of a Huckle Cat deploy, not optional.

**Morning brief / morning briefing.** The 5 AM PT composite Telegram message aggregated from every agent's morning output. Each agent writes its contribution to `cache/morning-brief-ready.txt`; the fleet-deliver cron at `0 12 UTC` assembles and sends. See [Fleet delivery path](#az).

**Pending action.** A staged mutation awaiting the operator's Confirm/Cancel button tap. Stored per-agent at `~/.clawford/{agent-id}-workspace/pending-actions.json` with a unique ID, a TTL (default 4 hours), and customizable button labels. Managed by `agents/shared/pending_actions.py`. See [Ch 18 § The pending-actions store](18-the-inbox.md#5-the-pending-actions-store-pending_actionspy).

**OpenClaw.** Retired. The pre-Clawford gateway platform the fleet ran on until 2026-04-15. Referenced in historical context (in commit messages, in retired-file tombstones) but not part of the live runtime. See [§ Retired terms](#retired-terms).

**Operator.** The human running the fleet. A Clawford fleet is a single-operator system — every agent is tuned to one operator's preferences, one operator's calendar, one operator's relationships. "The operator" is the guide's standard way to refer to the person the fleet exists to serve, without naming them.

**People file.** A per-person markdown file in the shared brain under `people/{email-slug}.md`. Contains name, email, phone, circle (family/work/friend/acquaintance), relationship_type, tone, context_notes, last_interaction, upcoming_interactions, and an array of durable facts. Written by [Huckle Cat's](14-huckle-cat.md) mining pipeline + maintained by `daily-refresh.py`.

**Persistent profile.** A browser profile directory that survives across Playwright or Camoufox sessions, preserving cookies + local storage so that an authenticated session doesn't need to re-login on every script run. Canonical consumer in the public fleet: [Huckle Cat](14-huckle-cat.md) (Google Messages).

**PKCE.** Proof Key for Code Exchange, the OAuth 2.0/2.1 extension that lets public clients use the auth-code flow without a client secret. The load-bearing trick for Tier 3 auth against hostile vendors: decode a captured id_token's `userIdentities` claim, find a PKCE-capable public client id, and use it to collapse a browser dance into a direct HTTPS call. See [Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice).

**Producer tool.** A tool in the `TOOLS` manifest that stages a [pending action](#az) instead of executing a mutation directly. The tool calls `pending_actions.stage()` and returns a `__pending_action__` marker that the dispatcher uses to auto-attach inline buttons. Examples: `propose_event_add` (Mistress Mouse), `propose_add_note` (Huckle Cat). See [Ch 18 § The tools.py pattern](18-the-inbox.md#the-toolspy-pattern).

**Post-liberation.** The state of the fleet after the Clawford liberation completed (2026-04-15). Synonymous with "the live runtime" in most contexts. Contrast with [pre-liberation](#az).

**Pre-liberation.** The state of the fleet before the Clawford liberation started (before 2026-04-02). Characterized by the OpenClaw gateway container, the exec-approvals allowlist, and the 600-second LLM cron timeout. Referenced in historical context only.

**Residential proxy.** A proxy service that routes traffic through residential IP addresses rather than datacenter IPs, to avoid vendor-side bot detection. The cheapest version is a SOCKS5 tunnel over Tailscale to the operator's home network; paid providers (DataImpulse, IPRoyal) offer sticky-port sessions as a fallback. See [Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice).

**Read tool.** A tool in the `TOOLS` manifest that has no side effects — it reads cached state and returns structured data for the LLM to use in composing a response. Examples: `get_fleet_health` (Mr Fixit), `get_events_for_day` (Mistress Mouse), `get_morning_nudge` (Huckle Cat). See [Ch 18 § The tools.py pattern](18-the-inbox.md#the-toolspy-pattern).

**Review-before-finalize.** The pattern where a pipeline generates a tiered review markdown and the operator reviews + edits before a `--finalize` flag commits anything durable. Used by [Huckle Cat's](14-huckle-cat.md#the-mining-pipeline) mining pipeline. Prevents bad-quality automated output from landing in the shared brain.

**Safeguard.** A deterministic Python check in `deploy.py` that either passes or fails, with no gray area. Ten active safeguards (1 Backup, 2 Source-cleanliness, 3 UPDATE confirm, 4 Drift detection, 5 Workflow banner, 6 Smoke test, 7 Manifest semantics, 9 Cron message discipline, 10 Config source classification, 12 pip-audit supply-chain). Two retired (8 exec-approvals baseline, 11 docker-compose.yml drift). See [Ch 19 § Defense layer 3](19-security-and-hardening.md#defense-layer-3-the-deploy-tool-safeguards).

**Script contract.** The three-rule contract every cron-invoked script in the fleet obeys: (1) always exit 0, (2) emit exactly one JSON line on stdout, (3) never execute shell. Enforced by `contract_wrap.py` at the import surface and by Safeguard 9 at the deploy boundary. See [Ch 19 § Defense layer 2](19-security-and-hardening.md#defense-layer-2-the-script-contract).

**Shared brain.** See [Brain](#az).

**Shared library.** `agents/shared/*` — the world-access layer, organized by Tier 1 (clean APIs), Tier 2 (stock Playwright), Tier 3 (hardened Camoufox), plus a handful of ops modules. See [Ch 06 — Infra setup](06-infra-setup.md) and [Ch 20 Category 1](20-scripts-and-configs.md#category-1-shared-library).

**Smoke test.** Safeguard 6 in `deploy.py`. A per-manifest command that runs after deploy and must exit 0 for the deploy to be marked successful. Typically `python3 scripts/heartbeat.py --dry-run`.

**`SOUL.md`.** Agent identity doc at the workspace root. Populated from `SOUL.md.example` during deploy and `chattr +i`-ed. The part of the agent that should never change without explicit operator intervention.

**Soft constraint.** A rule encoded as SOUL.md text (e.g., "never modify this file"). Useful as documentation for humans; **not** a security mechanism. An LLM given a direct instruction will happily violate a soft constraint. Hard constraints (OS-level file permissions + `chattr +i`) are the security layer. See [Ch 19 § The soft-constraint trap](19-security-and-hardening.md#defense-layer-1-os-level-immutability).

**Sticky port.** Residential-proxy feature where a specific port number binds to the same residential IP for the duration of a session, so sequential requests from the same script look like they originated from the same user. Port 10000 in the fleet's current setup.

**T-10 slack.** The 30-minute gap between a composition cron (`30 10 UTC`) and a delivery cron (`0 12 UTC`), or between `daily-refresh` (`0 10 UTC`) and `morning-relationship-nudge` (`30 10 UTC`). Slack for the composition run to finish before the downstream consumer fires. Do not eliminate the slack to "simplify" the schedule — the gap is load-bearing.

**TOTP.** Time-based One-Time Password. The 6-digit 2FA code generated from a seed secret, used in Tier 3 auto-MFA flows. Seed lives in `secrets.env` (chmod 600, separate from regular `.env`) as `{VENDOR}_TOTP_SECRET`, computed by `pyotp` at login time. See [Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice).

**`tools.py`.** Per-agent conversational tool manifest at `agents/{agent-id}/tools.py`. Exports two dicts: `TOOLS` (the LLM-visible function schemas) and `EXECUTORS` (the Python callables that implement each tool, including confirm executors that are not in `TOOLS`). See [Ch 18 § The tools.py pattern](18-the-inbox.md#the-toolspy-pattern).

**Tool-use loop (`tool_use.py`).** A sync two-turn loop that calls the LLM with a tool manifest, executes any function call the LLM picks, feeds the result back, and repeats until the LLM produces a text reply or the iteration cap (6) is hit. 90-second timeout per `infer()` call. Arguments echoed as JSON strings (empirical finding against the Codex backend). See [Ch 18 § The tool-use loop](18-the-inbox.md#3-the-tool-use-loop-tool_usepy).

**Tier 1 / Tier 2 / Tier 3.** The three tiers of the shared library, named for how hostile the target is. Tier 1 is clean APIs. Tier 2 is stock Playwright behind a persistent profile. Tier 3 is hardened Camoufox behind a residential proxy. Every agent consumer picks the lowest tier that works for its target. See [Ch 06 — Infra setup](06-infra-setup.md).

**`TOOLS.md`.** Per-agent tool inventory file. Lists every script the agent can run, what each does, and what each requires. Tracked in git (not `.example`) because it is operator-editable reference documentation, not LLM-editable identity.

**Workflowy routing rule.** The bright-line rule from [Ch 12 § The routing boundary](12-mistress-mouse.md#the-routing-boundary-with-sergeant-murphy): if a calendar event has a corresponding Workflowy item, it is a meeting and [Sergeant Murphy](13-sergeant-murphy.md) owns it; if not, it is an event and [Mistress Mouse](12-mistress-mouse.md) owns it. Both agents check Workflowy presence before acting on any event.

**Workspace.** Per-agent directory at `~/.clawford/{agent-id}-workspace/` on the VPS. Contains the agent's identity files, scripts, cache, state, credentials, and logs. Source of truth for every post-deploy agent runtime.

## Named incidents

Load-bearing war stories referenced by name across multiple chapters. Each one produced a design rule or a scar-tissue invariant that still shapes the live runtime.

**The 5x resend incident (2026-04-14).** [Sergeant Murphy](13-sergeant-murphy.md#the-5x-resend-incident). Post-meeting-scan cron used cache files as a delivery queue, re-sent the same debrief five times over 2.5 hours until the operator pulled the cron by hand. Rule: [cache-is-not-a-queue](#az). Silent prior on 2026-04-08 discovered during post-mortem.

**The probation episode (2026-04-11).** [Mr Fixit](09-mr-fixit.md#the-probation-episode). `chr()` approval incident that violated three agent rules and triggered a same-day brain transplant. Produced the five Diagnostic Discipline rules, the P1-P4 failure framework, and the pre-baked `retire.sh`.

**The stale-dates bug (2026-04-14 / 2026-04-21).** [Huckle Cat](14-huckle-cat.md#the-stale-dates-bug-and-the-refresh-crons). Morning relationship nudge flagged contacts the operator had just texted because the mining pipeline stamped `last_interaction` once at deploy time and never refreshed. Two-part fix: `daily-refresh` (2026-04-14) re-mines a 14-day window each morning covering Gmail-inbound + GCal + GMessages; `gmail-sent-mine` (2026-04-21) walks the Sent folder every two hours to catch outbound email `daily-refresh` misses. Second-order: 6-attendee cap on transcript-based refresh to prevent all-hands meetings from silently clearing the overdue list; the ✅ done button now also stamps `last_interaction = today`.

## Retired terms

Terms that used to matter in the pre-liberation fleet and are now historical. Flagged here so readers of old commit messages or older documentation can find what the term means without assuming it refers to live infrastructure.

**Exec approvals / exec-approvals allowlist.** Retired. The pre-liberation mechanism for gating what commands an agent was allowed to run. Safeguard 8 (which enforced the allowlist baseline) was retired. References in live code are bugs.

**OpenClaw.** Retired. The pre-liberation gateway container that sat between the agents and the world. Removed entirely during the post-OpenClaw migration. References in historical context (commit messages, retired-file tombstones) are fine; references in live code are bugs.

**`openclaw.json`.** Retired. The pre-liberation agent configuration file that lived inside the OpenClaw container. Does not exist post-liberation.

**`oc` / `oci` CLI.** Retired. The pre-liberation gateway CLI for managing agents (`oc agents add`, `oci cron list`, `oci approvals policy`). No post-liberation equivalent — the operator drives the fleet directly via `deploy.py`, `install-host-cron.sh`, `crontab`, `ssh`, and `codex infer`.

**600-second LLM cron timeout.** Retired. The pre-liberation hard limit on cron runtime. Drove the introduction of the [compose/deliver split](#az) pattern, which survives post-liberation because it also maps cleanly onto the [fleet delivery path](#az).

**`~/.openclaw/`.** Retired directory path. Renamed to `~/.clawford/` on 2026-04-15. References in live code are bugs; the regression guard at `agents/shared/tests/test_phase7b_openclaw_paths_retired.py` prevents them from recurring.

**`~/openclaw/` (no dot).** Retired directory path. The pre-liberation VPS env-file location. Deleted during the migration; `.env` now lives at `~/clawford/.env`.

**Safeguard 8 (exec-approvals baseline).** Retired. Enforced drift detection against a platform-level exec-approvals baseline that no longer exists. Tombstoned in `deploy.py` source comments.

**Safeguard 11 (docker-compose.yml drift).** Retired. Enforced drift detection against a `docker-compose.yml` that no longer exists (gateway container was removed). Tombstoned in `deploy.py` source comments.

## See also

- [Ch 01 — What is Clawford?](01-what-is-clawford.md) — the introduction
- [Ch 02 — What Clawford isn't](02-what-isnt-clawford.md) — the manifesto
- [Ch 06 — Infra setup](06-infra-setup.md) — the shared library + shared brain + host-cron runtime
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the deploy path and safeguard story
- [Ch 19 — Security and hardening](19-security-and-hardening.md) — the three defense layers
- [Ch 20 — Scripts and configs](20-scripts-and-configs.md) — the file-level reference
