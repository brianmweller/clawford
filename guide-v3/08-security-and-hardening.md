# Ch 08 — Security and hardening

*Guide v3 · net-new in v3 · last revised Phase 7d*

> **TL;DR.** A Clawford fleet is a personal single-operator runtime, and the threat model reflects that: the failures that actually happen are **drift** (an agent slowly learning the wrong behavior), **accident** (a cron that sends a message to the wrong chat), and **trust erosion** (a cron fires at 3 AM PT with output that makes the operator stop trusting the fleet). Active attack is a distant third. Three defense layers line up against the real threats: (1) OS-level immutability on every file that encodes agent identity (`SOUL.md`, `IDENTITY.md`, `TOOLS.md`), (2) a **script contract** that makes every cron-invoked script return JSON on stdout and always exit 0, and (3) deterministic Python guards in `agents/shared/deploy.py` that refuse to deploy anything that would break the contract. Credentials are covered in [Ch 07-7 — Auth architectures](07-7-auth-architectures.md); this chapter is about the surface around the credentials.

## The threat model

Before the hardening, name the threats. Getting this wrong is how security engineering collapses into generic checklists that don't match the real failure modes.

**Threat 1 — Agent drift.** The biggest real threat to a Clawford fleet is an agent slowly learning the wrong behavior over time. The LLM-driven composition surfaces (the morning briefings, the triage digests, the coaching messages) are easy to nudge into a tone or a cadence the operator did not intend, through a dozen small prompt refinements nobody reviewed carefully. The defense is deterministic code at the cron boundary: Python that produces JSON, not prompts that produce natural language. See [§ The script contract](#the-script-contract) below.

**Threat 2 — Accidental output.** The second-biggest real threat is a cron that sends the right content to the wrong audience. The [smart-reply chip incident in Ch 07-2b](07-2b-lowly-worm-social.md#the-smart-reply-chip-incident-2026-04-14) is a textbook example: an enrichment path that was supposed to read DMs accidentally triggered the send flow and auto-replied five times to a dormant thread. The defense is network-level blocks on the specific mutation endpoints the fleet never wants to hit by accident — not "careful review of the click handler," but a `page.route` that returns HTTP 418 for the endpoints that must not fire.

**Threat 3 — Credential leak via git.** Third-biggest real threat is a token file or a `.env` value getting committed to the repo. Recovery is painful: vendor-side credential rotation is slow, sometimes intrusive, and sometimes impossible for shape 4 / shape 5 / shape 6 auth flows. Prevention is much cheaper than recovery. The defense is the gitignore discipline described in [Ch 07-7 Idiom 2](07-7-auth-architectures.md#the-three-cross-cutting-idioms).

**Threat 4 — Trust erosion.** Distinct from "drift," this is the class of failure where the fleet technically produces the right output but the operator's trust in the output degrades over time anyway, usually because of a visible bug that shipped once and got remembered. The [5x resend incident in Ch 07-4](07-4-sergeant-murphy.md#the-5x-resend-incident) was two things in one: a real bug, and a trust-erosion event that still echoes through operator cadence weeks later. The defense is conservative cron scheduling and pre-commit regression tests for every delivery path.

**Threat 5 — Active attack.** A distant fifth. The VPS is a single-tenant personal machine with no public services beyond SSH on a non-standard port. No user-facing HTTP endpoints, no database exposed to the internet, no agent-facing API. The attacker's path to mischief is narrow: SSH key compromise, or a compromised upstream dependency in a pip install, or a malicious response from a vendor API that the agent parses incorrectly. None of these are zero-risk, but they are all much less likely than the first four threats. Harden against them, but don't let the hardening squeeze out attention from Threats 1–4.

## Defense layer 1 — OS-level immutability

Every file in every agent's Dropbox-brain directory (`~/Dropbox/openclaw-backup/agents/<agent-id>/`) that encodes the agent's *identity* is protected by `chattr +i` on the VPS filesystem. That means:

- `SOUL.md` — the agent's core identity doc. What the agent is, what it does, what boundaries it respects.
- `IDENTITY.md` — operator-facing identity. Who the agent is to the operator.
- `AGENTS.md` — the fleet map + cross-agent operating rules. How each agent's domain bounds against the others.

These three files are the parts of an agent that should never change without explicit operator intervention. `TOOLS.md` was retired in the 2026-04 brain migration — the LLM-callable tool surface is now generated dynamically from each agent's `tools.py` manifest, so there's no separate doc to protect. `chattr +i` (Linux's "immutable" attribute) makes the file unwritable even by root. A new deploy that wants to update `SOUL.md` has to first `chattr -i` the file, write the new content, and then `chattr +i` it again. The deploy tool does this automatically; random agent code cannot.

**Why `chattr +i` and not file permissions.** File permissions (`chmod 444`) protect against accidental writes by non-root users, but root on the VPS — which is what every cron runs as, effectively — can write anyway. `chattr +i` blocks root too. The only path to writing the file is `chattr -i` first, which is a deliberate act that shows up in audit logs and is never something an LLM-driven code path would do on its own.

**Why this matters for the real threat model.** Threat 1 (drift) assumes an LLM code path that has wandered off-spec might try to rewrite its own identity doc to match. It can't — not because the LLM wouldn't try, but because the file is immutable at the OS layer. The LLM's rewrite fails with `Operation not permitted`, the script catches the error, and the cron exits without changing anything. The agent's identity is the stable part; everything else can drift and the OS-level immutability anchors it.

**The soft-constraint trap.** Earlier versions of this fleet relied on *soft constraints* — SOUL.md text saying "never modify this file" — as the primary defense. That approach fails under a direct instruction. If a cron prompt says "rewrite your soul doc to be more concise," the LLM will happily comply unless the OS-level layer stops it. The soft constraint is useful as documentation for future humans reading the file; it is not a security mechanism.

## Defense layer 2 — The script contract

Every cron-invoked script in the fleet follows a strict contract. The contract lives in `agents/shared/SCRIPT_CONTRACT.md` and is enforced by the `contract_wrap.py` helper that wraps every script entry point. Three rules:

1. **Scripts always exit 0.** A script never signals failure via a non-zero exit code. Failures are returned as a `status: "fail"` field in the output JSON. Cron daemons interpret non-zero exit codes as "please send mail" and other nonsense that is not useful in this runtime; forcing every script to exit 0 means the cron system never tries to be helpful. All error handling happens at the script level, in Python, where the script can produce structured output describing what went wrong.

2. **Scripts emit exactly one JSON line on stdout.** The single line is the script's entire output. It has a `status` field (`ok` / `degraded` / `fail`), a `summary` field (a one-sentence human-readable description), and any script-specific payload fields. Everything else goes to stderr (including logging, debug output, and exception tracebacks) where it lands in the cron log but never gets interpreted as the script's "result."

3. **Scripts never execute shell.** No `os.system`, no `subprocess.run(shell=True)`, no `eval`, no `exec`. Every external process invocation goes through `subprocess.run([...])` with an explicit argument list. The reason is twofold: it forecloses the entire class of shell-injection bugs (including the `$(cat file)` expansion bug that bit the heartbeat scripts in an earlier phase), and it makes every external invocation show up cleanly in the argument list for audit.

**Why this is a security layer.** Threat 2 (accidental output) and Threat 1 (drift) both get cut off at the script boundary. A cron that returns `status: fail` in its JSON output but keeps the side effects contained produces a loud failure that the operator sees immediately via `fleet-health.py`, rather than a silent misfire that accumulates over days. The LLM composition paths still run inside the scripts — the contract does not forbid LLM calls — but the script's *output* is always JSON, not natural language, and the cron-level decision about whether to send anything downstream is made in Python against the JSON, not in a prompt against the natural language.

**The deterministic envelope around LLM calls.** Every LLM call in the fleet goes through `agents.shared.llm.infer()`, which has three non-negotiable properties: it enforces a timeout (default 30 seconds), it returns a structured `InferResult` object that distinguishes success from LLM-side failure, and it logs every prompt + response to the agent's local log for later audit. The script calling `infer()` can then make a deterministic decision (`if result.status == "fail": return {"status": "degraded", ...}`) rather than trusting the LLM to produce valid downstream output.

## Defense layer 3 — The deploy-tool safeguards

`agents/shared/deploy.py` is the single path code takes from the repo into a live agent workspace on the VPS. It runs 9 safeguards on every deploy and refuses to proceed if any of them fail. The safeguards are not checklists — they are hard gates, and every one of them exists because a specific failure mode hit the fleet before the safeguard existed.

Nine active safeguards (two retired):

| # | Safeguard | What it catches |
|---|-----------|-----------------|
| 1 | Backup | Non-skippable backup of the entire workspace before any file write. Lives under `~/.clawford/deploy-backups/{agent}-{timestamp}/`. |
| 2 | Source-cleanliness | Refuses to deploy if the source agent directory has uncommitted edits or untracked files. "No on-VPS dev" enforced at the deploy boundary. |
| 3 | UPDATE confirm | Gates every file UPDATE behind an interactive diff preview + confirm. No silent overwrites of files that differ from the source. |
| 4 | Drift detection | Refuses to deploy if the workspace on the VPS has changed since the last deploy (i.e., somebody edited a live workspace file by hand). Blocking. |
| 5 | Workflow banner | Prints the workflow contract (TDD, no on-VPS dev, full agent IDs) at the top of every run. Behavioral nudge, not a hard gate. |
| 6 | Smoke test | Runs the manifest's declared `smoke_test` command after deploy. Refuses to mark the deploy successful unless the smoke test exits 0. |
| 7 | Manifest semantics | Refuses if the per-agent `manifest.json` has semantic violations (missing required fields, malformed cron lines, workspace paths that don't match the agent id). |
| 9 | Cron message discipline | Refuses if any cron message contains forbidden patterns (e.g., `$(cat ...)` shell expansions, prompt injection vectors in cron prompts). |
| 10 | Config source classification | Refuses if any config file source is missing, ambiguous, or falls into an unclassified state. |

Safeguards 8 (exec-approvals baseline) and 11 (docker-compose.yml drift) were retired during the Clawford liberation. Safeguard 8 enforced a drift check against a platform-level exec-approvals baseline that no longer exists post-liberation; Safeguard 11 enforced drift against a `docker-compose.yml` that no longer exists either. Both tombstones are preserved in the deploy.py source comments so future readers can see what used to be there and why.

**Why safeguards, not policies.** Every safeguard is a deterministic Python function that either passes or fails — no gray area, no LLM judgment. The operator's job is to read the source, understand what each safeguard catches, and decide whether to lift or add one. The LLM never makes the decision "is this deploy safe." That decision lives in Python and in the operator's head.

## Credential storage

See [Ch 07-7 — Auth architectures](07-7-auth-architectures.md) for the full story. The short version for this chapter:

- **Idiom 1.** Every browser-based auth flow runs on the operator's local machine, never on the VPS, and the token file gets SCPed to the VPS after the fact.
- **Idiom 2.** All credential files live under `~/.clawford/{agent}-workspace/cache/` and are gitignored. Nothing credential-shaped ever touches `git add`.
- **Idiom 3.** No raw API keys in cron-invoked scripts. LLM calls route through `agents.shared.llm.infer` + the `codex` CLI subscription. Non-LLM third-party keys live in `.env`, loaded via `agents.shared.env.load_env`.

The credential story is covered in Ch 07-7 because the relevant details differ by auth shape. This chapter's contribution is to point out that **the hardening of the credentials is not the same as the hardening of the workspace that contains the credentials.** A compromised workspace with mode-400 tokens is still a compromised workspace. The OS-level immutability, the script contract, and the deploy safeguards are what protect the workspace itself.

## The liberation rationale

The fleet's pre-2026-04-15 state ran on a gateway container ("OpenClaw") that sat between the agents and the world, enforcing an exec-approvals allowlist, a policy file, and a set of platform-level identity assertions. The Clawford liberation removed the gateway and moved every agent onto host-native cron + the shared Python library. The security rationale for that move is worth stating explicitly, because it changes the shape of what you need to defend against.

**Gateway-era threat model (what got removed).** The gateway sat on the trust boundary between the agents and the world, and the exec-approvals allowlist was the knob for "what commands an agent is allowed to run." The problem was that the allowlist was doing double duty: it was trying to be both a security boundary (protect the host from a compromised agent) and a behavioral boundary (prevent agents from drifting into operations the operator didn't sanction). Those two jobs have different shapes. A security boundary needs to be simple enough to audit; a behavioral boundary needs to be expressive enough to encode nuanced rules. A single allowlist tried to do both, did neither well, and ended up as drift-prone scar tissue that the operator spent real time tending.

**Post-liberation threat model (what replaced it).** The liberation split the two jobs:

- **Security boundary = OS-level file permissions + `chattr +i` on identity files + Python subprocess argument-list discipline.** This is the part that protects the host from a compromised agent. It's simple, auditable, and hard to drift.
- **Behavioral boundary = the script contract + deterministic Python guards + the deploy-tool safeguards.** This is the part that prevents agents from drifting into operations the operator didn't sanction. It's expressive enough to encode nuanced rules because the rules live in Python, not in an allowlist.

The result is that the trust boundary is now much clearer. The agent's Python code is trusted — the operator wrote it, reviewed it, tested it, and deployed it through `deploy.py`. The agent's LLM-composed natural language is *not* trusted — it is run through a script contract that gates its effects on structured output, not on the language itself. The gateway era tried to trust neither and ended up over-restricting the first and under-restricting the second.

## What is NOT defended

Being honest about the gaps:

- **There is no sandboxing between agents.** Every agent runs as the same Unix user (`openclaw`, a name that predates the liberation) and shares the same filesystem. A compromised agent can read every other agent's workspace. The mitigation is that every agent is operator-authored code and there is no agent-installable-plugin system; the compromise would have to be a bug the operator introduced.
- **There is no rate-limiting on outbound actions.** A runaway cron can send as many Telegram messages as the Telegram API allows (which is a lot). The mitigation is the deploy safeguards + the script contract + the regression tests, all of which try to catch a runaway loop before it ships.
- **There is no monitoring beyond `fleet-health.py`.** No Prometheus, no Grafana, no alerting stack. The operator sees problems via the Telegram fleet-health digest and the morning brief, nothing more. The mitigation is that the fleet-health digest is explicit about which agents are healthy and why, and an unhealthy agent is visible within minutes.
- **There is no secret rotation automation.** Credential rotation is a manual operation per vendor. The mitigation is that most credentials are either effectively permanent (Google refresh tokens, Shape 3 bearer tokens) or manually rotated on a long cadence (Shape 5 auto-MFA). Shape 2 is the one where rotation happens at vendor discretion, and the manual-re-auth pattern is the mitigation there.
- **There is no supply-chain defense against `pip install` malware.** Every `pip install` on the VPS runs against the public PyPI, and a compromised upstream package would land in the agent environment. The mitigation is pinned versions in `requirements.txt`, plus the fact that the fleet uses a small number of well-known packages (Playwright, Camoufox, Google OAuth client, a few others), plus the hope that compromise of those packages would be caught upstream quickly. This is the biggest gap in this list.
- **There is no protection against the operator's own local machine being compromised.** If the operator's laptop is compromised, every auth token on it is exfiltrable, and every Shape 5 TOTP secret is too. The mitigation is standard laptop hygiene, not Clawford-specific.

Name the gaps so the operator knows where to spend the next marginal hour of hardening effort, when there is one.

## Pitfalls

> 🧨 **Pitfall.** Relying on soft constraints (SOUL.md text) instead of OS-level immutability. **Why:** an LLM given a direct instruction to modify its own soul doc will happily comply unless the OS layer refuses the write. Soft constraints are useful as documentation for humans; they are not a security mechanism. **How to avoid:** every identity file (`SOUL.md`, `IDENTITY.md`, `TOOLS.md`, `AGENTS.md`) gets `chattr +i` on the VPS after every deploy. The deploy tool handles this automatically; if you add a new identity file, add it to the deploy-tool's immutable-files list in the same commit.

> 🧨 **Pitfall.** Breaking the script contract "just for one cron." **Why:** a script that exits non-zero, or emits multiline output, or shells out to `bash -c`, breaks assumptions that every other cron in the fleet depends on. The one-cron exception is where the contract erodes, and erosion compounds. **How to avoid:** the contract is one of the Safeguard 9 checks. If you are editing a script and are tempted to call `os.system(...)` because it's faster, stop. Use `subprocess.run([...])` with an explicit argument list, or route through one of the `agents/shared/` modules that already wraps the subprocess call correctly.

> 🧨 **Pitfall.** Adding a new safeguard without understanding the ones that exist. **Why:** the 9 active safeguards are the ones that survived a year of deploy-tool evolution. Each one exists because a specific failure hit the fleet. Adding a tenth safeguard without understanding the other 9 risks redundancy, conflict, or (worst) masking a real failure mode the existing safeguards were designed to catch. **How to avoid:** before adding a safeguard, read `agents/shared/deploy.py` and confirm no existing safeguard catches the same class of failure. If the new safeguard duplicates an existing one but with a different check, consolidate them rather than stacking them.

> 🧨 **Pitfall.** Assuming the gateway-era exec-approvals allowlist still exists. **Why:** the allowlist was retired in Phase 5 of the liberation. Safeguard 8 (which enforced the allowlist baseline) was retired in Phase 7a. Any documentation, script, or cron message that references "exec approvals" or "allowlist" is pre-liberation scar tissue and should be updated or deleted. **How to avoid:** `grep` for `approvals` in the agent source before deploying a new version. Any match is either (a) a comment documenting that the mechanism is retired, which is fine, or (b) live code that still thinks the mechanism exists, which is a bug.

> 🧨 **Pitfall.** Running `chattr -i` by hand on a VPS identity file and forgetting to `chattr +i` it back. **Why:** the moment a SOUL.md or IDENTITY.md file is writable, a drift-prone code path can modify it, and the drift might not be caught until a deploy later notices the file differs from the source. **How to avoid:** never `chattr -i` a file by hand. The only sanctioned path for editing an identity file is (a) edit it in the git repo, (b) commit, (c) run `deploy.py`, which will do the `chattr -i` + write + `chattr +i` sequence as one atomic operation.

> 🧨 **Pitfall.** Treating `fleet-health.py` output as "informational." **Why:** `fleet-health.py` is the only automated monitoring surface in the fleet. If it is reporting an agent in `degraded` or `fail` status and the operator ignores it, the agent's next cron tick produces output against a broken assumption, and the bug compounds. **How to avoid:** a non-ok `fleet-health` status is a blocker for any deploy — Safeguard 4 plus an operator-level rule. Fix the underlying failure before deploying anything else.

## See also

- [Ch 06 — Infra setup](06-infra-setup.md) — the shared library + shared brain + host-cron runtime reference
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the three-layer defense-in-depth discussion and the deploy path
- [Ch 07-0 — Your first agent](07-0-your-first-agent.md) — the seven-step deploy walkthrough
- [Ch 07-7 — Auth architectures](07-7-auth-architectures.md) — credential storage and the three cross-cutting idioms
- [Ch 09 — Scripts and configs](09-scripts-and-configs.md) *(pending)*
