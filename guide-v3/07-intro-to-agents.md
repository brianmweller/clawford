![Clawford](../assets/Clawford2.png)

# Intro to agents

*Last updated: 2026-04-15 · Reading time: ~18 min · Difficulty: moderate*

**TL;DR**

- A Clawford agent is a directory of markdown files plus a `manifest.json`. No hidden sidecar, no remote state. If you can read eight files, you can read an agent.
- The fleet draws a hard line between LLM reasoning (compose, rank, judge) and deterministic Python (scrape, auth, execute). Knowing which side of the line a piece of work belongs on is half the design work on a new feature.
- Every scheduled job in the fleet fires from host crontab — no gateway runtime, no skill system, no platform cron with a 600-second ceiling. The 5 AM PT fleet path (populate at `30 10 * * *` UTC → deliver at `0 12 * * *` UTC) is the convention for anything that contributes to the morning brief.
- Every script a cron calls must follow the **script contract**: print one JSON line with a `status` field, always `exit 0`. Exit codes are for operators, not for LLMs.
- Defense in depth lives in the script contract + OS-level identity locks + `deploy.py` safeguards. No exec-approvals policy, no allowlist. The rationale is in [Ch 06](06-infra-setup.md); the historical version lived at a platform layer that no longer exists.

## An agent is a directory of files

Every agent in Clawford is a directory that holds:

- **Eight markdown workspace files** — the agent's durable identity and operating rules.
- **A `manifest.json`** — the single source of truth for how the agent deploys: bot, crons, scripts, on-disk state.
- **A `scripts/` directory** of deterministic Python (and occasionally shell) that the LLM invokes to touch the real world — browsers, HTTP, filesystem, Gmail, payment portals.
- **A `tests/` directory** of red/green TDD tests for everything in `scripts/`.

If there's ever confusion about what an agent *is*, open `agents/fix-it/` in a file browser. The answer is "that directory." No hidden sidecar, no remote state.

## The eight workspace files

The workspace files are the agent's durable identity. The cron runner loads whichever ones a given task needs; `codex infer()` only reads them when a script asks it to. None of them are auto-loaded in the background — there is no background.

| File | What it holds |
|---|---|
| **`SOUL.md`** | Values, boundaries, operating model. The "why" of the agent. Made immutable on the VPS after deploy (`chattr +i`) because prompt injection will try to rewrite it otherwise. |
| **`IDENTITY.md`** | Name, emoji, tone, catchphrase. Who the agent *is*. Also immutable. Gitignored — real files have PII; only `.example` templates are tracked. |
| **`TOOLS.md`** | Which scripts are available, what they do, when to use which. The LLM reads this to answer "how should I accomplish task X?" |
| **`AGENTS.md`** | Hard rules, role definition, config architecture, the fleet roster — so one agent knows who its coworkers are. |
| **`USER.md`** | The human's name, timezone, preferences, communication style. Keeps the agent calibrated to a specific person instead of to a generic user. Also gitignored; only the `.example` is tracked. |
| **`HEARTBEAT.md`** | The lightweight 30-minute checklist — things to verify on every heartbeat tick without burning the full LLM budget. |
| **`MEMORY.md`** | Persistent lessons learned from experience — *"NEVER re-enable X, ALWAYS use Y."* Where scar tissue lives. |
| **`CRONS.md`** | The per-agent cron schedule reference, in markdown, for humans reading the repo. Authoritative scheduling lives in `manifest.json` and `ops/scripts/install-host-cron.sh`; `CRONS.md` is the readable mirror. |

`SOUL.md` and `IDENTITY.md` get made immutable at the filesystem level (`chattr +i`) after deploy. Soft prompt-level rules are not strong enough — an LLM will happily rewrite its own soul if asked to directly. The OS-level lock is the backstop.

## `manifest.json` is the whole agent, declared

Each agent has a `manifest.json` sitting next to the workspace files. It is the single source of truth for how the agent gets deployed. Trimmed shape:

```json
{
  "agent_id": "shopping",
  "display_name": "Hilda Hippo",
  "workspace": "~/.clawford/shopping-workspace",
  "telegram": {
    "account": "shopping",
    "bot_token_env": "SHOPPING_BOT_TOKEN"
  },
  "config_files": [
    { "src": "SOUL.md", "immutable": true },
    { "src": "IDENTITY.md", "immutable": true },
    { "src": "TOOLS.md" },
    { "src": "AGENTS.md" },
    { "src": "USER.md" },
    { "src": "HEARTBEAT.md" },
    { "src": "MEMORY.md" },
    { "src": "CRONS.md" }
  ],
  "scripts": [
    "scripts/heartbeat.py",
    "scripts/delivery-digest.py",
    "scripts/costco-token-daemon.py"
  ],
  "state_files": [
    { "path": "grocery-list.json", "seed_if_absent": { "items": [] } }
  ],
  "crons": [
    {
      "name": "delivery-digest",
      "cron": "30 10 * * *",
      "message": "run python3 scripts/delivery-digest.py"
    }
  ],
  "smoke_test": { "script": "scripts/heartbeat.py", "max_wait_s": 60 }
}
```

When `agents/shared/deploy.py <agent-id>` runs, it reads this file and does the full install: copies workspace files into the VPS workspace, seeds state files, syncs the shared library, and captures a pre-deploy backup tarball. It also runs nine safeguards — see [Ch 06](06-infra-setup.md) for the full inventory with outage stories.

> A few legacy fields (`approvals.allowlist`, `approvals.policy`, `approvals.security`) still appear in older `manifest.json.example` copies. They were gated on an OpenClaw-era approvals concept that no longer exists and are ignored by the current deploy tool. A cleanup pass in a later liberation phase removes them.

> ⚠️ **Warning.** `manifest.json` is gitignored and `IDENTITY.md` and `USER.md` are gitignored for the same reason: real values contain PII (family names, calendar IDs, bot tokens). The checked-in versions are `*.example` files. On first-time setup, `deploy.py <agent> --bootstrap-configs` scaffolds each missing real file by copying its `.example` sibling and prepending a `CLAWFORD_BOOTSTRAP_UNEDITED` sentinel. Hand-edit the dummy values, delete the sentinel line, redeploy. Safeguard 10 refuses to deploy any agent that still has the sentinel on line 1.

## How a deploy actually moves code to production

There is exactly one canonical path from a code change in your editor to that change running on the VPS. Skipping any step in this sequence will burn an hour and confuse things.

```bash
# 1. Edit, commit, push from the dev box.
git add agents/shopping/scripts/delivery-digest.py
git commit -m "shopping: handle out-of-stock items"
git push origin master

# 2. SSH to the VPS and pull.
ssh openclaw@<vps>
cd ~/repo
git pull --ff-only origin master

# 3. Run deploy.py FROM THE VPS SHELL.
python3 agents/shared/deploy.py shopping --yes-updates
```

**The trap to avoid:** running `deploy.py` on the dev box. The tool has no `scp`, no `rsync`, no `ssh` — it just writes files into `\$HOME/.clawford/<agent>-workspace/` on whatever box runs it. On the VPS that path is the production workspace. On a laptop it's a mirror that nothing reads. A `--dry-run` from a laptop will happily enumerate planned changes against the dead mirror and print a clean plan, which is exactly the kind of false confirmation that makes the trap dangerous.

The first time I hit this, I spent an hour re-deriving why a local `deploy.py` kept complaining about six missing config files (`IDENTITY.md`, `TOOLS.md`, `AGENTS.md`, and friends). The hydrated PII versions of those files don't exist on the dev box — they live only on the VPS, gitignored — and the local mirror had no `fix-it-workspace/` directory at all because the laptop had never been a real deploy source. A `grep` for `scp\|rsync\|ssh` inside `deploy.py` came back empty and the fog cleared. The local invocation was writing to a directory nothing on the production host would ever read.

**Useful exception.** A local `--dry-run` against a fully-hydrated checkout can validate a manifest change without touching the VPS. In practice, SSH'ing to the VPS and running the dry-run there is faster than hydrating PII files locally, so this exception almost never gets used.

### When the VPS working tree is dirty

`install-host-cron.sh` runs occasionally on the VPS and can leave the working tree slightly dirty (mode flips on host wrappers, untracked log files, or stash artifacts from past sessions). Before pulling, check with `git status`. If there's noise:

```bash
git stash push -m "pre-deploy noise <YYYY-MM-DD>"
git pull --ff-only origin master
git stash list   # inspect the stash
git stash drop   # if the stash was just noise (the usual case)
```

The dirty-tree state is almost always benign — host wrapper mode bits, untracked logs, or a half-applied edit from a prior session. Inspecting the stash before dropping it costs ten seconds and catches the rare case where it isn't noise.

### When deploy.py refuses on a drift violation

`deploy.py` Safeguard 4 (workspace drift) refuses to overwrite a workspace file that has changed since the last deploy's recorded backup. The intent is to catch hand-edits made directly on the VPS that haven't been committed back.

Nine times out of ten, the "drift" is a false positive — the workspace content is byte-identical to the repo source, the safeguard is matching on a stat-only difference. Diff the file against the repo first:

```bash
diff ~/.clawford/<agent>-workspace/<file> ~/repo/agents/<agent>/<file>
```

If the diff is empty, re-run with `--accept-drift`:

```bash
python3 agents/shared/deploy.py <agent> --yes-updates --accept-drift
```

If the diff is non-empty, the drift is real — someone edited the workspace file directly on the VPS, and that edit is not in git. Pull the workspace file back into local git first (`scp` it to the dev box, commit it as-is to preserve the live state, edit locally, push, pull, deploy normally). Don't blindly `--accept-drift` a real drift; you'll lose the live edit on the next deploy.

## LLM vs deterministic — where the line sits

The most important design rule in Clawford is that **the LLM does not touch the real world directly**. The LLM reasons; deterministic code acts.

- **LLM work (via `agents.shared.llm.infer`):** composing digests, ranking items, summarising threads, making judgment calls on free-form natural language, drafting Telegram messages.
- **Deterministic work (plain Python):** scraping websites with Playwright, handling auth and multi-factor re-auth, calling the Gmail API, writing to files, talking to external APIs, parsing HTML, validating JSON, writing state.

The seam between the two is plain JSON. A script runs as a subprocess, emits one JSON line on stdout with `{"status": "ok" | "error" | "degraded", ...}`, and exits 0. The orchestrator — another Python script, not an LLM — decides what to do next. If any step in the middle needs reasoning (summarise this transcript, pick the top 5 items), the orchestrator calls `infer()` with a specific prompt and a narrow schema, and takes the result back into deterministic territory.

This line is what keeps the system debuggable. When something goes wrong, the first question is always "is it the script or the LLM?" and the answer is almost always visible in whatever the script printed to stdout. If the script emitted `{"status": "ok", "orders": [...]}` and the Telegram message was confused, the bug is above the seam. If the script emitted `{"status": "error", "error": "auth_failed"}` and the orchestrator ignored it, that's an orchestration bug. Either way, the JSON is the contract.

### When to reach for the LLM

The default is deterministic Python. Reach for `infer()` only when the input is genuinely free-form natural language that keyword matching can't handle reliably.

**Pure Python wins** for structured calendar events, structured order records, structured invite fields, and any case where the input has a schema the code already knows. Templated output. Rule-based classification. Most of the fleet's work lives here.

**LLM wins** for raw newsletter bodies, raw family chat transcripts, the content of a meeting debrief, and any case where the input is prose with no schema. Summarisation. Sentiment. "Is this a concerning email or a routine one."

This isn't a philosophical stance — it's a cost-and-failure discipline. LLM calls are the parts of the fleet that produce weird outputs on bad days. Keeping the deterministic paths deterministic shrinks the surface area where weird outputs can happen.

## Host crons and the 5 AM PT fleet path

Every scheduled job in the fleet fires from the host crontab. No container runtime, no platform cron, no 600-second ceiling, no exec-approvals allowlist that tightens on an upgrade. Just `crontab -l` and the host Python interpreter.

The contract installer at [`ops/scripts/install-host-cron.sh`](../ops/scripts/install-host-cron.sh) owns the contents of the crontab. It reconciles the live crontab against a list of `CONTRACT_ENTRY` lines declared per agent and evicts stale lines automatically when a schedule, path, timeout, or env-var token changes between runs. No manual `crontab -e`. Ch 06 covers the runtime in detail.

### The fleet path for morning briefs

Every agent that contributes to the morning briefing follows the same shape:

1. **Populate** at `30 10 * * *` UTC (3:30 AM PT, ~1.5h of slack).
2. Write plain text atomically to `<workspace>/cache/morning-brief-ready.txt`.
3. Do **not** call `telegram.send_telegram()` directly from the morning orchestrator.
4. A single fleet aggregator at `0 12 * * *` UTC (5:00 AM PT) reads every agent's cache file and sends one consolidated brief.

The fleet-path discipline matters because without it, five agents each send their own early-morning message at five slightly different times and the human wakes up to a notification storm instead of one actionable digest. The 3:30 populate / 5:00 deliver split gives every agent an hour and a half to be late without breaking the brief, and the atomic-write-to-cache pattern means a partially-failed agent cleanly drops out of the brief instead of corrupting it.

Non-morning crons don't need to follow the fleet path — a pre-meeting alert or an engagement poll goes out when it needs to. The fleet path is specifically for anything that contributes to the single consolidated morning message.

## The shared library

Agents don't reinvent the world-access layer. Everything an agent needs to touch anything outside its own workspace goes through `agents/shared/*`, organised by how hostile the target is:

- **Tier 1 — clean APIs.** `telegram.py`, `google_oauth.py`, `llm.py`, `heartbeat_base.py`, `brain.py`.
- **Tier 2 — stock Playwright.** `playwright_profile.py` for LinkedIn, Google Messages Web, and similar.
- **Tier 3 — hardened Camoufox behind a residential proxy.** `camoufox_proxy.py` + `retry_policy.py` for Costco, Amazon, any retailer with fraud scoring.

Ch 06 walks through each module with intent and consumer list. The short version: before writing a line of integration code, decide which tier the target belongs in and use the module that matches. Code built in the wrong tier eventually gets rewritten.

## The script contract

Every script an orchestrator (or host-cron wrapper) invokes must follow one shape:

1. **The final non-blank stdout line is a single JSON object** with at least `{"status": "ok" | "error" | "degraded"}`.
2. **Always `exit 0` from main**, even on failure. Catch the exception, print an error-shaped JSON object, then exit 0.
3. **Stderr is free.** Use it for debug logs — nothing parses it, so it won't interfere with the stdout contract.
4. **Run as a bare `python3 <absolute-path>`.** No shell wrappers, no pipes, no redirects, no `sh -c`, no `; echo $?`.

The full spec and a skeleton template live in [`agents/shared/SCRIPT_CONTRACT.md`](../agents/shared/SCRIPT_CONTRACT.md). The test harness in `agents/shared/tests/test_script_contract.py` enforces the contract — statically by walking every manifest's cron messages and rejecting forbidden shell operators, and at runtime by running every script in isolation and asserting the output shape.

The reason the contract is pedantic is scar tissue. An earlier platform version had a hardcoded exec preflight that rejected any `python3 <...>` command matching shell operators — `;`, `&&`, output redirects, `sh -lc`, exit-code capture. When an LLM running a cron session reflexively wrapped a command as `python3 script.py; printf "EXIT:%s" \$?` to "also check the exit code," the command got hard-rejected, and *before* the contract existed, exactly that wrapping cascaded the entire six-agent fleet into approval-blocked errors in a single morning. The durable fix had two sides: scripts started self-reporting status via JSON (removing the LLM's *reason* to wrap), and cron messages started opening with an explicit *"run this bare, do not append anything, do not capture exit codes"* preamble (removing the *temptation*). Both sides are load-bearing.

Safeguard 9 in `deploy.py` enforces the pattern blocklist on every deploy. [Ch 06](06-infra-setup.md) has the full story.

## Defense in depth

An earlier version of this chapter had a long section justifying a permissive exec-approvals policy. The policy existed because the platform of the day had a fragile allowlist that regressed every few releases. The policy is gone because the platform is gone.

The defense in depth that replaced it is structural and doesn't need per-agent tuning:

1. **OS-level immutability on identity.** `SOUL.md` and `IDENTITY.md` are `chattr +i` after deploy. An agent cannot rewrite its own values even if a prompt-injection attack tells it to, because the filesystem refuses.
2. **The script contract.** Scripts always `exit 0` and report status via JSON. Combined with cron messages that forbid shell operators (Safeguard 9), there's no path for an LLM to catch a failed command via a nonzero exit and try to "recover" it by escalating.
3. **`deploy.py` safeguards.** Nine active checks that run before any file touches the VPS: source clean, workspace drift, compose drift, manifest validation, config-source resolution, pre-deploy backup, post-deploy smoke test, cron-message hygiene, diff preview. Each one exists because of a specific past outage. Ch 06 has the inventory.

None of the three layers is a replacement for the others. They compose — every mistake the fleet has seen in production is caught by at least one of them.

## A few things that will bite you

### Google OAuth — auth on a laptop, not on the VPS

Google's OAuth flow requires a browser, and the VPS doesn't have one. The pattern: run the auth flow locally on a laptop, grab the resulting `token.json`, and `scp` it to the VPS. Also: add the Google account as a **test user** on the Cloud project *before* the first auth — Google's consent screen rejects non-listed users during testing mode, with an unhelpfully generic error. Use a **Desktop** OAuth client type, not Web — Web clients require an HTTPS redirect URI the VPS doesn't have. Every Google-touching agent (meetings-coach, family-calendar, connector) shares this flow.

### Never `claude -p` from inside an agent cron

Agent crons never shell out to the Claude CLI. The LLM entry point is `agents.shared.llm.infer()`, which routes through `codex` against a ChatGPT Plus subscription. Two reasons. First: determinism — an agent calling a CLI calling another LLM stacks so many sessions that an error in the middle becomes unreadable. Second: terms of service — Anthropic's Claude Code legal-and-compliance docs explicitly prohibit third-party developers from routing requests through Free, Pro, or Max plan credentials on behalf of their users. Wiring an agent up to `claude -p` against a Pro or Max plan is picking a fight you will lose.

The rule: agents talk to `infer()` and to their scripts; they don't talk to other LLM CLIs.

### Don't log into the VPS to "just quickly fix" a script

See the three invariants at the bottom of [Ch 04 — VPS setup](04-vps-setup.md). The deploy tool's drift detection (Safeguard 4) will notice and refuse subsequent deploys until the drift is reconciled. The fast fix always costs more than it saves.

## See also

- [AGENTS-PATTERN.md](../AGENTS-PATTERN.md) — the repo's canonical list of workspace files, deployment invariants, and human-facing rules. Keep this open while reading Ch 07-0 through 07-6.
- [`agents/shared/SCRIPT_CONTRACT.md`](../agents/shared/SCRIPT_CONTRACT.md) — the full script contract, the skeleton template, and the migration guide for pre-contract scripts.
- [`agents/shopping/manifest.json.example`](../agents/shopping/manifest.json.example) — the canonical manifest example with real cron shapes.
- [Ch 06 — Infra setup](06-infra-setup.md) — `deploy.py`'s safeguards, the shared library tiers, the shared brain, the host-cron runtime.
- [Ch 02 — What Clawford Isn't](02-what-clawford-isnt.md) — the decision doc that explains why the runtime looks like this and not like the platform it used to sit on top of.
