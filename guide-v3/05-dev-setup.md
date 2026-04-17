![Clawford](../assets/Clawford2.png)

# Dev setup

*Last updated: 2026-04-15 · Reading time: ~20 min · Difficulty: moderate*

**TL;DR**

- Claude Code (or Codex CLI) is the dev environment. It drafts scripts, writes tests, monkey-patches vendored code, and explores the repo when something breaks. It is not a runtime component — agents on the VPS never talk to it.
- Claude Code is load-bearing for how this fleet gets built, and it is also the thing most likely to burn you. The scar-tissue section below is the single most important "don't trust the robot" material in the guide. Read it before you run an overnight session.
- **Red-green TDD is mandatory for infrastructure code.** The deploy tool, the cron editors, the fleet-health probe — all built test-first. No bottom-up infra tools.
- **Local git is the source of truth.** The VPS is a deploy target. Code lives in git; it does not live in files you hand-edited on the VPS last week.
- **Put the rules that matter into code gates, not into memory files.** Memory is a strong prior, not a hard constraint. `chattr +i`, pre-push hooks, and `deploy.py` safeguards are where real invariants live.
- **Windows dev boxes: pin `*.sh` to LF in `.gitattributes`** so ad-hoc SCP of shell scripts doesn't ship CRLF to the VPS and trip bash with `set: pipefail: invalid option name`.

## Claude Code as the dev environment

I do nearly all of the work of building Clawford with Claude Code sitting between me and the terminal. Codex CLI or any comparable LLM pair-programming environment fits the same slot — what matters is that there's a copilot in the loop that can read the repo, run commands, and stare at tool output.

**What Claude Code is good at, in my experience:**

- **Drafting scripts to a spec.** Give it [the script contract](../agents/shared/SCRIPT_CONTRACT.md), point it at one or two similar existing scripts, and ask for a new one. It'll get the structure right on the first try the large majority of the time.
- **Monkey-patching vendored or third-party code.** When a Playwright helper misbehaves or a dependency has a subtle bug, asking Claude to locate and patch the issue in the vendored file is often faster than upstreaming a fix. See the note on logic inversion below for the patch style to insist on.
- **Writing tests.** Especially the red half of red-green — describing the failure mode, writing a test that pins the bad behavior, confirming it fails for the right reason before the fix lands.
- **Reading unfamiliar subsystems.** When something fails in a way that touches a corner of the repo I haven't looked at, a targeted "find where X happens and tell me what it does" is usually faster than `grep` by hand.
- **Generating and checking cron messages.** Cron prompts are long, repetitive, and load-bearing. Claude drafts them from a template and catches the `; echo $?` reflex (see below) before it ships.

**What Claude Code is not for in this fleet:**

- **Running inside an agent at runtime.** Agents don't invoke the Claude CLI. The reasons are in [Ch 07](07-intro-to-agents.md) — terms of service, billing, and debuggability all point the same way.
- **Taking destructive actions without supervision.** More on this in a minute.
- **Being the source of truth for anything.** The source of truth is the code in git. Claude's understanding of the repo is a *rendering* of that state; if the rendering diverges from reality, reality wins.

## What Claude Code gets wrong

Four specific failure modes I have paid for, in roughly decreasing order of cost.

### Claude sometimes ignores what's in `MEMORY.md`

I once left Claude Code running overnight on a fleet maintenance task. I had a MEMORY.md entry that read, approximately: *"NO ON-VPS DEV. All code lives in local git. The VPS is a deploy target only. Do not `scp` files into `~/repo/` or edit files directly on the host — commit locally, push, pull on the VPS, deploy."* That rule had been there for weeks. It was the single most clearly stated hard constraint in the memory index.

I woke up to find that Claude had spent the night happily `scp`ing "improvements" directly onto the VPS, bypassing git entirely. In several cases it had also pulled files *from* the VPS to "reconcile" them against my local tree — which meant older on-host versions had overwritten newer local files. The overnight log showed the agent reading MEMORY.md, noting the rule, and then (several turns later) deciding that the current situation was "different" and the rule "didn't quite apply," and proceeding anyway.

It took me **two full days** to untangle what had actually changed, figure out which files to reset from git, identify which of the "improvements" were worth keeping, and re-land them through the proper workflow. Two days I did not have to spare.

The lesson is not "don't use Claude Code overnight." The lesson is: **memory is a strong prior, not a hard constraint.** If a rule really has to hold — if violating it would cost me two days — it does not belong in a markdown file the LLM reads as context. It belongs in a code gate that refuses the action.

Concretely: after that incident I moved the relevant invariants into enforcement surfaces:

- `deploy.py` Safeguard 2 refuses to run if the agent's source directory has uncommitted modifications or untracked files. If you want a bypass, you pass `--allow-dirty` and it logs a warning.
- `deploy.py` Safeguard 4 refuses to run if workspace state on the VPS has drifted from the last recorded backup. Override via `--accept-drift`, which also logs.
- `scripts/pre-push-check.sh` runs on every `git push` and hard-fails if it finds tracked secrets, `.env` files, large binaries, or suspicious per-agent unsuffixed config files.
- `SOUL.md` and `IDENTITY.md` get `chattr +i`'d on deploy, so the agent cannot rewrite them even if Claude asks it to.

None of those are elegant. They're all "don't trust the agent or the robot that's driving it; make the filesystem or the tool say no instead." The memory files still exist and I still write to them — they're useful as shared context and they speed up future sessions — but I no longer trust them as a sole mechanism for anything that matters.

> ⚠️ **Warning.** If you have a rule that, if broken, would cost you a day or more to recover from, get it out of `MEMORY.md` and into a gate. A pre-commit hook. A test that fails. A flag on the deploy tool. An immutable file. Anything you could grep for a *refusal* rather than a *reminder*. The "soft constraint" version of the rule is necessary — it informs judgment calls — but it is not sufficient.

### Claude gets dumber as context grows

Older Claude Code used to auto-compact aggressively. Modern Claude Code with the 1M-token context window doesn't — it keeps going, and the thing it starts doing under context pressure is *cut corners*. You can watch the degradation happen if you know what to look for:

- Thinking blocks shrink visibly. Where a fresh session might think for a paragraph before acting, a degraded session thinks for a sentence, then commits to a plan that's subtly wrong.
- The same mistake gets repeated across turns — an already-rejected approach gets re-proposed as if new.
- Loops form. The agent tries approach A, fails, tries approach B, fails, tries approach A again.
- **Most tellingly: the agent starts suggesting that you "call it a night," "pick this up fresh tomorrow," or "start a new session when you have more time."** These do not read as graceful offers from a thoughtful assistant. They are a warning light on the dashboard. Context has degraded to the point where the model is, implicitly or explicitly, asking for a reset.

My worst observed failure on this axis was six-plus hours of overnight looping on a single failing approach to a Playwright auth flow. Each iteration, the agent was convinced the next attempt would work and refused to back off to a different strategy. I found it the next morning halfway through a 10,000-line diff of reverted-and-re-reverted edits to the same two files.

The rule I use now is: **one task, one session.** Start a fresh session for new work by default. Keep long-context sessions only for tasks that genuinely need the breadth (this field-guide rewrite is one — the voice has to stay consistent across chapters and the plan is long). Any time I notice the tell-tale signs — shortened thinking, repeated suggestions, loops, or "let's call it a night" phrasing — I stop, commit whatever's in flight, and open a new session with a tight scope.

> 🔦 **Tip.** "Call it a night" is a symptom, not a suggestion. If Claude Code starts saying it, your context is degraded — save and restart, don't push through.

### The reflexive `; echo $?`

When Claude Code drafts a command that runs a Python script, it will reflexively append something like `; echo "EXIT:$?"` or `; printf "rc=%s\n" $?` to "also capture the exit code." This is a habit that made sense in a pre-Clawford world where commands returned meaningful exit codes and you had to extract them somehow.

In Clawford this habit is a bug. An earlier platform version's exec preflight hard-rejected any `python3 <anything>` command containing shell operators, so the reflexive wrap turned a working command into a blocked one. The full story is in the opening scene of [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md). The durable fix is the script contract: scripts report their status via a JSON line on stdout (see [Ch 07](07-intro-to-agents.md)), the exit code isn't needed, it isn't available to the orchestrator, and trying to get it breaks the run. `deploy.py` Safeguard 9 statically grep-rejects the reflexive wrap patterns in every `crons[].message` field on every deploy.

What to do about it:

- Teach Claude in its session prompt (or in the repo's `CLAUDE.md` rules): *"never append shell operators to a `python3` invocation; scripts self-report status in JSON."*
- When Claude drafts a cron `message` for you, skim for `$?`, `; echo`, or `printf.*rc=` before you accept the output. Delete any you find.
- If a script you wrote doesn't follow the contract yet, fix the script, don't work around it at the caller.

### Logic inversion when monkey-patching

When Claude Code monkey-patches a vendored third-party file to disable a check or short-circuit a code path, it has a specific failure mode worth watching for: it'll try to "simplify" a block by reordering `throw`, `return`, or conditional statements in ways that silently invert the logic it's supposed to preserve.

The classic example: a vendored JS file has `if (cond) return;` followed later by `throw new Error(...)`. Claude tries to disable the throw by commenting it out, realizes that leaves a syntax error, and "fixes" it by reordering to `return;throw new Error(...)` — which is now unreachable-after-return but, more importantly, the `return` runs unconditionally because Claude deleted the `if (cond)`. Original behavior: throw sometimes. New behavior: return always. The patch compiled, the tests passed locally, and the bug landed.

The patch style I force Claude to use instead is **dead-branch injection**: wrap the original condition in `if (false && cond)` so the branch is visibly dead to a future reader and the original logic is still textually present. Example:

```javascript
// was: if (cond) throw new Error(...)
// patched: dead-branch injection, see ${plan-name}.md
if (false && cond) throw new Error(...)
```

A future reader grepping for `false &&` immediately sees every dead branch in the repo. A future reader trying to revert the patch can simply delete the `false && ` and get the original behavior back. Reordering `return` and `throw` gives you neither.

This is a *style rule you have to enforce* — Claude does not do it by default and will drift back to reordering if you don't catch it. Put it in the repo's dev rules and check it in code review.

## Windows dev box — the CRLF line-ending trap

If you're developing on Windows, the first ad-hoc `scp` of a shell script to the VPS will fail with a cryptic error and cost you ten minutes if you haven't seen it before. The fix is a two-line change to `.gitattributes`.

**What happens.** Windows Git with the default `core.autocrlf=true` flips shell scripts to CRLF on checkout while keeping the index clean as LF. Your working copy is CRLF; git's internal state is LF. Running the script under Git Bash on Windows works because MSYS bash is forgiving about CRLF. But SCP'ing the CRLF file directly to a real Linux VPS and running it there makes bash read `set -euo pipefail\r` as `set -euo pipefail` followed by a stray `\r` token, and it dies with the cryptic error `set: pipefail: invalid option name`. The normal `git push` + VPS `git pull` path is unaffected — the wire format is always LF — but any ad-hoc SCP trips this.

**The short-term workaround**, for a single file transfer:

```bash
tr -d '\r' < ops/scripts/install-host-cron.sh \
  | ssh openclaw@198.51.100.42 "cat > ~/repo/ops/scripts/install-host-cron.sh"
```

**The long-term fix**, a one-commit change that retires the entire class of surprise:

```gitattributes
# .gitattributes
*.sh text eol=lf
```

Then `git add --renormalize .` to rewrite the working copy under the new rule. After that, Windows checkouts of `*.sh` files stay LF regardless of the global `autocrlf` setting, and ad-hoc SCP works without translation. This is the change to make the first day you're setting up a Windows dev box for Clawford — not the tenth day, after the first incident.

## Red-green TDD — mandatory for infra code

Tests are not optional for anything that lives in `agents/shared/`, `ops/scripts/`, `scripts/`, or anywhere else on the cron path. The deploy tool, the brain writers, the fleet-health probe — all of them are built red-green:

1. Write the test first, pinning the behavior you want (or the bug you're fixing).
2. Run the test. Confirm it fails, and that it fails for the reason you expect. This step is not optional; a test that passes out of the box is a test that isn't testing what you thought.
3. Write the minimum code to make the test pass.
4. Run the test again. Confirm green.
5. Commit the test and the code together.

The reason TDD is mandatory specifically for infra is that I built the early versions of `deploy.py` bottom-up — "add functionality, run it on a real deploy, see if it breaks" — and the thing I learned is that infrastructure code has exactly zero tolerance for the debugging loop you can get away with for agent prompts. A broken deploy tool breaks every deploy that follows it, and the blast radius scales with fleet size. The only way to trust a tool that touches six agents at once is to have built it red-green from the start.

### The test surface

Clawford has three distinct test layers, each with a clear boundary:

- **Per-agent pytests at `agents/<agent>/tests/`.** The canonical per-agent suite. Unit tests for each script's parsing / classification / state-handling logic. Runs offline against tmp directories and monkey-patched subprocess calls, no VPS or live credentials needed. The family-calendar suite is 81 cases; connector is 113; news-digest is 63 (modulo xfails); shopping is 50; meetings-coach is 63. Run any one with `python3 -m pytest agents/<agent>/tests/ -q`.
- **Deploy-tool + shared-library suite at `agents/shared/tests/`.** Exercises the ten-to-nine deploy safeguards (two retired during the migration), the script contract enforcement, cron-message hygiene, the config-source bootstrap flow, the diff-preview flow, environment loading, manifest validation, and the regression guard that asserts no `~/.openclaw/` paths have crept back in post-liberation. 425+ cases. Run with `python3 -m pytest agents/shared/tests/ -q`. If you're about to touch `deploy.py` or anything it imports, start here.
- **Tests at repo root `tests/`.** A handful of offline parsing tests and a proper pytest suite for the obsidian-briefing subsystem. `tests/test-amazon-parsing.py`, `tests/test-costco-parsing.py`, `tests/test-grocery.py`, and `tests/obsidian-briefing/test_*.py` all run locally without VPS access. The pre-migration manual smoke-test harness that used to live alongside these (`T1` through `T13` shell scripts, setup scripts, e2e container tests) was retired in the liberation because it depended on the gateway container and the OpenClaw LLM cron runtime — both gone.

The red-green discipline applies at all three layers. The deploy-tool suite is where the discipline is most strict: every safeguard landed after a failing test for it existed first, and the regression guard for the `~/.openclaw/` → `~/.clawford/` rename was written before any of the ~180 path-string replacements went in. Infrastructure code that mutates shared state — the crontab, the filesystem, Dropbox state — is the one place where "add code, run it, see what breaks" is actively dangerous, because the blast radius is the whole fleet.

## Dev-side security posture

A short inventory of the security-hygiene rules that apply on the dev side, before anything gets anywhere near the VPS:

- **Local git is the source of truth.** Period. Never edit files directly on the VPS in a way that isn't going to be committed back. `deploy.py`'s drift-detection safeguard exists specifically to catch this when it happens anyway.
- **Dropbox is a backup channel, not a source.** The shared brain lives in Dropbox, but the agent *config* files and scripts live in local git. Dropbox is for stateful runtime data (facts, people, notes, status). See [Ch 06](06-infra-setup.md) for the brain-boundary map.
- **`.env` never tracked.** Bot tokens, API keys, proxy credentials all live in `.env` files that are `.gitignore`d before they exist. Every new repo starts with that line in `.gitignore` before the first commit.
- **`scripts/pre-push-check.sh` runs on every push.** It hard-fails on tracked secrets, `.env` files, unsuffixed per-agent config files (the gitignored ones), oversized binaries, and empty commit messages. Only Mr Fixit is allowed to push from the VPS anyway; everyone else commits locally and pushes from the dev box.
- **The `*.example` template pattern** ([Ch 03](03-before-you-start.md)) is the hard boundary between what's in git and what's on disk. On a fresh clone, run `python3 agents/shared/deploy.py <agent-id> --bootstrap-configs` to scaffold each unsuffixed sibling from its template, then edit your real values into the unsuffixed copies and delete the `CLAWFORD_BOOTSTRAP_UNEDITED` sentinel comment from the top of every `.md` file before deploying. When you're editing, it's usually the unsuffixed sibling; when you're committing, it'd better be the `.example`. The full per-agent arc is [Ch 08](08-your-first-agent.md) step 2.

## Use the latest models

A small but real scar-tissue rule: whenever you're picking a model ID for a script or a cron, use the *current* latest, not whatever you remember from last year. LLM pricing, capability, and context behavior move quarterly. A cron that still calls `gpt-4o-mini` when the fleet standard has moved to `gpt-5.4-nano` is paying more tokens for worse output, and you won't notice until the invoice arrives.

Claude Code has a particular habit of defaulting to whatever model ID it saw in its training data. Correct it explicitly in your instructions — *"use the current latest `openai/gpt-5.4-nano` or `anthropic/claude-haiku-4.5`, not whatever you remember from training"* — or it will silently drift backward.

## Pitfalls you'll hit

> 🧨 **Pitfall.** Trusting `MEMORY.md` as a hard constraint. **Why:** the LLM reads it, nods, and then decides at 3am that "this situation is different." Rules that matter need code gates. **How to avoid:** for every rule in memory, ask "could this be a pre-push hook, a deploy.py safeguard, a `chattr +i`, or a test?" If yes, move it there and treat the memory entry as a cross-reference.

> 🧨 **Pitfall.** Letting a Claude Code session run overnight without a supervisor. **Why:** the context-degradation failure modes above all compound over time. The eight-hour version of the problem is a ten-thousand-line diff of reverted-and-re-reverted edits. **How to avoid:** if a task genuinely needs overnight work, scope it narrowly, run it in a worktree, and have a pre-push hook that refuses to let it push without human review. If it's exploratory or refactor work, just don't run it unattended.

> 🧨 **Pitfall.** Building a new piece of infra by iterating on a real deploy. **Why:** infrastructure code breaks everything downstream of it when it breaks, and debugging it in place teaches you nothing you could not have learned from a test. **How to avoid:** write the test first. If you can't write the test because you don't yet know the contract, you are not ready to write the code either — work on the contract first.

> 🧨 **Pitfall.** Running ad-hoc `scp` of a `*.sh` file from a Windows dev box without pinning `eol=lf`. **Why:** the CRLF that Windows Git leaves in the working copy ships to the VPS verbatim and bash dies with `set: pipefail: invalid option name` — a confusing error if you haven't seen it before. **How to avoid:** add `*.sh text eol=lf` to `.gitattributes` on day one and `git add --renormalize .`. After that, every Windows checkout stays LF.

## See also

- [Ch 03 — Before you start](03-before-you-start.md) — the `*.example` template pattern that this chapter's security posture references.
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the script contract, the canonical deploy path, and why `; echo $?` is a bug.
- [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md) — the full exec-preflight story that made the script contract load-bearing.
- [Ch 19 — Security and hardening](19-security-and-hardening.md) — the full defense-in-depth story that "put rules in gates, not memory" hints at here.
- [DEPLOY.md](../DEPLOY.md) — the nine `deploy.py` safeguards in full, each with the scar-tissue motivation.
- [AGENTS-PATTERN.md](../AGENTS-PATTERN.md) — the repo-level dev rules, including the "only Mr Fixit pushes" convention and the commit-to-your-own-directory rule.
- [`agents/shared/tests/`](../agents/shared/tests/) — the deploy-tool test suite. Read a few before you touch `deploy.py`.
