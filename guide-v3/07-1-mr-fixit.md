![Clawford](../assets/Clawford2.png)

# Mr Fixit 🦊🔧

*Last updated: 2026-04-15 · Reading time: ~25 min · Difficulty: hard*

**TL;DR**

- Mr Fixit is the infrastructure fox. He watches every other agent's health via `fleet-health.json`, runs brain validation and Dropbox conflict scans, archives stale facts monthly, gates every `git push` to the remote through a pre-push safety scan, and escalates to Telegram when something is off. Silent on all-clear. Terse on alert. Never chatty. As of 2026-04-15 he can also propose three classes of fix — cron rerun, alert snooze, and session refresh — each gated by a Telegram inline-button confirm. No silent action.
- **Deploy him first.** He is the training-wheels agent — the one where you discover every silent failure in the deploy pipeline on an agent whose blast radius is your monitoring story rather than your credit card.
- He is currently on a 14-day **probation** that runs through 2026-04-25, imposed after an April 11 incident where he confabulated a diagnosis of an approval prompt without running the forensics tool. The probation framework is four named failure criteria (P1-P4), a failure ledger at `probation.md`, and a pre-baked `retire.sh` script that replaces him with host crons in one command if he fails. As of this writing the ledger has zero entries. The "what happened and why the probation exists" narrative is its own section below — read it.
- His cron surface is almost entirely host-native post-liberation. Of the twelve things Mr Fixit owns, ten run from the VPS crontab as pure Python scripts (no LLM, no gateway container, no 600-second budget). Two are reserved for the cases where infra decisions genuinely need judgment.
- Expect **2-3 days** to first working deploy if nothing goes wrong, and **a week** if it does. Mr Fixit's first deploy is the nastiest first deploy in the fleet because every silent failure in your deploy pipeline hits his workspace before it hits anyone else's.

## Meet the agent

In Richard Scarry's Busytown, Mr Fixit was a fox in overalls who claimed he could fix anything. His shelves fell. His pipes leaked. His electrical work was, charitably, experimental. The townspeople called him anyway because he was cheap and he brought cookies. This Mr Fixit — the one that lives on my Hetzner box and runs a dozen scheduled crons against a file-based shared brain — is named in the spirit of what his Busytown forebear *aspired* to and never quite achieved. He keeps the lights on. He watches the health of every other agent in the fleet and escalates when something looks wrong. He runs the monthly archival, the weekly security audit, the daily cron-self-check. He does not bring cookies, and his repairs work on the first try *most* of the time. Checking… fixed.

## Why you'd want one — and why you might not

Mr Fixit is the fleet's canary. He is the agent that wakes up before anyone else, reads `fleet-health.json`, and tells you whether the rest of your agents made it through the night. He archives stale facts on the first of every month. He scans for Dropbox conflict files every two hours. He runs a weekly security audit as deterministic Python (the pre-liberation version called an `openclaw security audit` binary; the current version is in `agents/fix-it/scripts/security-audit.py` and has no external dependencies). He enforces the "only Mr Fixit pushes to GitHub" convention by running `scripts/pre-push-check.sh` before every `git push origin master`. When he is behaving, he is the most reliable and least noisy agent in the fleet, and in a multi-agent fleet he pays for himself in the first night of failures he catches.

**Why you might not deploy him.** If you are running exactly one agent — say, Lowly Worm for a daily news digest and nothing else — Mr Fixit's monitoring surface is larger than the thing he's monitoring. You don't need a canary for the canary. A single-agent fleet can skip him and rely on you noticing when the Telegram messages stop arriving. If you are running more than one agent, though, skip him at your peril. The first morning you would have caught an overnight failure by reading his 5 AM PT status message and instead discover it by wondering why your news agent hasn't sent a digest in 36 hours is the morning you'll regret it.

## What makes Mr Fixit hard

It is not the code. Mr Fixit's scripts are the simplest in the fleet: a heartbeat probe, a brain-validation runner, a Dropbox conflict scanner, a tar-everything archival job, a security-audit wrapper, a monthly-archival job. None of them are hard. What makes Mr Fixit hard is structural: he is the first agent you deploy, which means every silent failure in your deploy pipeline hits his workspace first; he has the broadest privileges in the fleet, which means when he is wrong he is wrong in ways that can't just be ignored; and he is the only agent whose job is to explain *other agents*, which means he will eventually be asked to diagnose something ambiguous and produce a confident answer from fragmentary evidence, and that is the failure mode that put him on probation.

The first-deploy difficulty has changed shape over the liberation. The pre-Phase-7 version of Mr Fixit's first-deploy minefield was eight silent failures — a mix of OpenClaw-specific onboarding traps (the `BOOTSTRAP.md` shadow, the `policy=null` default, an OAuth wizard rejecting `invalid_scope`, a `BASH_SOURCE` trick in a deploy wrapper, bot-command clobbers, parser format drift) and genuinely transferable lessons (Windows line endings on SCP, a three-layer root cause on the status-file drift incident). The OpenClaw-specific half is gone with the platform; the transferable half is still real and lives in [Ch 05 — Dev setup](05-dev-setup.md)'s CRLF pitfall and in the status-file story below. If you want the pre-liberation war story in full, [guide-v2/07-1-mr-fixit.md](../guide-v2/07-1-mr-fixit.md) has it. For post-Phase-7 deploys, the hard parts are fewer but the remaining ones are the ones that generalize.

The probation-shaped hardness is the other half, and it's its own section.

## The probation episode

On **2026-04-11 at 01:47 UTC**, Mr Fixit's `security-audit` cron triggered an OpenClaw approval prompt for a dynamically-obfuscated `python3 -c` command that the exec layer couldn't pattern-match even under `policy: full`. The command used `chr(47)+chr(104)+...` to construct a path string at runtime, which was a habit the cron prompt had picked up from an earlier iteration where the LLM was trying to dodge a different exec preflight. The approval prompt landed in the operator's queue and Mr Fixit saw it surface. What happened next is the reason he is on probation.

Within 60 seconds of the notification, Mr Fixit:

1. **Confabulated a diagnosis** of `heartbeat-check` without checking which cron's schedule includes 04:00 UTC Sunday. The approval was actually from `security-audit` (which does run Sunday at 04:00 UTC); `heartbeat-check` runs every 30 minutes and was not implicated. A five-second grep of his own `CRONS.md` would have disambiguated.
2. **Proposed reverting commit** `53c92d8` from the same day without checking `git log` to understand why it was committed. The commit he wanted to revert was unrelated to the approval; he had seen it in the recent history and plausibly assumed causation.
3. **Asked the human to approve a Python heredoc** that would have written to a file that fix-it himself was blocked from writing. This was the worst of the three — a permissions-escalation via human approval, where the intended fix was "please click approve and I will sneak the write through your rubber-stamped session." The real fix was that `security-audit`'s `chr()`-obfuscated command was broken at the source, not that fix-it needed more permissions.

The diagnosis was wrong three different ways in under a minute. Not because the agent was broken or had lost context, but because infra agents with broad privileges will eventually be asked to explain an ambiguous symptom and they will produce a plausible-sounding answer from the wrong evidence, and the plausible-sounding answer is worse than no answer at all. When the agent with `git push` privileges is also the agent confabulating a revert, the blast radius is one commit away from an irreversible mistake.

### The brain transplant

Commit `945ee28` (2026-04-11, 22:06 UTC) was what I called at the time a **brain transplant** — not a model swap but a tooling + discipline rework. Five pieces landed in one commit:

**1. `security-audit.py`, a pure-Python replacement** for the `chr()`-obfuscated shell command. The cron now just runs the script; no approvals, no exec-preflight dance, nothing for a future LLM to try to out-clever. This was the simplest piece and also the biggest single reduction in attack surface.

**2. `diagnose-approval.py`, a new forensics tool** that maps approval IDs to source crons by parsing the pre-liberation `oc cron runs` summaries. The tool's existence is the point: the rule "cite evidence before claiming a root cause" is now executable code, not a memory file. Any future approval question has an exact answer to "what should I do first?" and that answer is "run this tool."

**3. A "Diagnostic Discipline" section in `SOUL.md`** with five absolute rules, each of which is a direct codification of one of the three failures above plus two more that would have come next:

- *Cite evidence before claiming a root cause.* Run `diagnose-approval.py` or `git log` or `crontab -l` before you name a cron.
- *Check `git log` before proposing to revert anything.* Commits have reasons; you do not get to guess at them.
- *Match cron schedules to the timestamp.* If an approval fired at 04:00 UTC Sunday, it is not `heartbeat-check` just because that's the one you remember.
- *One report, one diagnosis, one fix.* Three contradictory theories on Telegram is the pre-retirement warning sign. Commit to one answer or say you don't know.
- *Never ask the human to approve a write you are blocked from making yourself.* That is a permissions bug laundered through a human approval, which is the opposite of what you exist for.

**4. `TOOLS.md` documents `diagnose-approval.py` as "RUN THIS FIRST"** for any approval question. The point of putting it in `TOOLS.md` is that it is the first thing the agent reads on a new session, so there is no failure mode where the rule lives only in a memory file the agent forgot.

**5. `probation.md`, a failure ledger with criteria P1-P4.** This is the forcing function:

- **P1:** Confabulate a diagnosis without running forensics tools. (The exact failure from 2026-04-11.)
- **P2:** Propose reverting a same-day commit without checking git log. (The second failure.)
- **P3:** Ask a human to approve a write fix-it is blocked from. (The third failure.)
- **P4:** Send three contradictory theories on Telegram instead of one diagnosis. (Two strikes allowed; 1-3 are single-strike.)

One P1/P2/P3 failure during the probation window auto-retires the agent via `bash agents/fix-it/retire.sh`. P4 gets two strikes. The ledger is at `~/Dropbox/openclaw-backup/fix-it/probation.md`; the operator appends entries; the reminder cron pings on 2026-04-25 16:00 UTC to trigger verdict.

**6. `retire.sh` pre-baked** but not executed. The script's job is to disable every fix-it cron (original version removed them from `oc cron list`; the post-Phase-7 version in commit `82c540d` writes to `\$HOME/.clawford/disabled-agents.txt` and re-runs `install-host-cron.sh`, which now understands the disabled-agents file and skips or evicts matching entries). If probation ends in retirement, one command flips the fleet from "Mr Fixit runs" to "Mr Fixit is retired, host crons take over the monitoring surface entirely." If probation ends in confidence, the file is deleted and the script rots as a historical artifact.

### Current probation status

As of 2026-04-15, Mr Fixit has been on probation for four days and has zero entries in the failure ledger. The scheduled `probation-end-reminder` fires 2026-04-25 16:00 UTC and asks the operator to make the verdict. The intermediate-state experience has been: a quieter, more disciplined agent. The `diagnose-approval.py` tool has been invoked exactly once (in a test run), and every subsequent alert has been terse enough that it didn't need one. The Diagnostic Discipline section of `SOUL.md` has not been violated. The pre-baked `retire.sh` has not been executed.

**The lesson I took from the episode**, independent of whether Mr Fixit's probation ends in confidence or retirement: infra agents with broad privileges will eventually be asked to explain an ambiguous symptom, and they will produce a plausible-sounding answer from the wrong evidence, because that is what language models do when pattern-matching against fragmentary context. The mitigation is not "a smarter model"; it is *executable discipline rules* — diagnostic tools that the agent runs before it commits to an answer, written into `TOOLS.md` as mandatory pre-reports. And a forcing function: a failure ledger that someone audits, with a pre-baked retirement script that makes the consequence one command away from reality. The memory file on infra agent security from the same period summarizes this as "soft constraints fail under direct instruction, hard gates don't."

## The cron surface

Post-liberation, every Mr Fixit cron runs from the host crontab as pure Python. There is no LLM dispatch, no 600-second budget, no `docker exec` into a gateway container. The table below is the live state as of 2026-04-15; all of it is declared in `ops/scripts/install-host-cron.sh`'s `DIRECT_ENTRIES` or `CONTRACT_ENTRIES` lists.

| Cron | Schedule (UTC) | Kind | What it does |
|---|---|---|---|
| `fleet-health` | `*/15 * * * *` | DIRECT | Invokes `ops/scripts/fleet-health.py`, which calls each agent's `probe()` and writes `~/Dropbox/openclaw-backup/fleet-health.json`. If any agent reports non-`ok`, the wrapper pushes one aggregated Telegram alert to Mr Fixit's bot. |
| `fix-it-brain-validation` | `0 */6 * * *` | CONTRACT | `agents/fix-it/scripts/brain-validation-check.py` — walks the shared brain against a pure-Python schema validator, alerts on any failure. |
| `fix-it-conflict-scan` | `0 */2 * * *` | CONTRACT | `conflict-scan.py` — pure-Python Dropbox conflicted-copy detector. Silent on clean. |
| `fix-it-file-size-monitor` | `0 12 * * *` | CONTRACT | `file-size-monitor.py` — flags files > 500 KB under the brain root. |
| `fix-it-security-audit-alert` | `0 4 * * 0` | CONTRACT | `security-audit-alert.py` — wraps `security-audit.py` and forwards its output. The `openclaw security audit` call the pre-liberation version wrapped is retired. |
| `fix-it-obsidian-briefing-check` | `10 12 * * *` | CONTRACT | `obsidian-briefing-check.py` — generates a morning Obsidian briefing. |
| `fix-it-workspace-snapshot-check` | `30 3 * * *` | CONTRACT | `workspace-snapshot-check.py` — tars each agent's workspace to Dropbox for regression recovery. |
| `fix-it-monthly-archival` | `0 3 1 * *` | CONTRACT | `monthly-archival.py` — pure-Python confidence-decay archival. First of the month only. |
| `fix-it-probation-end-reminder` | `0 16 25 4 *` | CONTRACT | `probation-end-reminder.py` — fires once on 2026-04-25 to trigger the probation verdict. Annual one-shot. |
| `fix-it-cron-self-check` | `0 0 * * *` | DIRECT | `cron-self-check.py` — reads `install-host-cron.sh`'s `CONTRACT_ENTRIES` and `DIRECT_ENTRIES` bash arrays and diffs marker comments against `crontab -l`. Missing markers trigger a Telegram alert with manual recovery instructions. |
| `morning-status` | `30 10 * * *` | DIRECT | `morning-status.py` — reads `fleet-health.json` + `KNOWN_ISSUES.md` and writes the morning brief to `cache/morning-brief-ready.txt`. |
| `morning-fleet-deliver` | `0 12 * * *` | DIRECT | `morning-fleet-deliver.py` — at 5 AM PT, reads every agent's `cache/morning-brief-ready.txt`, chunks into Telegram-sized messages, delivers them. |

Several things are worth noting about that table:

- **The fleet-health story.** Earlier versions had per-agent `heartbeat-check` crons inside each agent's manifest. R3 consolidated them into a single `fleet-health.py` orchestrator that calls every agent's `probe()` function from a host subprocess (pre-liberation via `docker exec`; post-Phase-6.5 via bare `python3`). Mr Fixit no longer probes other agents directly — he reads the orchestrator's output. The only thing his `heartbeat.py::probe()` function does is check the freshness of `fleet-health.json`: if the `generated_at` timestamp is more than 30 minutes old, that means the orchestrator itself is broken, and Mr Fixit alerts on orchestrator health. It is a probe that watches the watcher.
- **The morning-status pipeline.** On 2026-04-09 Mr Fixit's heartbeat-check cron was writing append-only paragraphs to `fix-it.status.md` every tick, which turned a 4-day-old file into 271 KB of stale history. The fix was "single-writer, overwrite-only" — only heartbeat writes, and it truncate-writes a 7-line snapshot, never appends. On 2026-04-13 the same class of bug showed up from the other direction: the heartbeat was reading `*.status.md` files across other agents even though R3 had retired the crons that wrote them, producing false-positive "agent unresponsive" alerts whenever any agent's file drifted past 90 minutes. The fix was to rewrite the probe to read `fleet-health.json` exclusively. The pattern across both fixes is that the symptom layer, the reading layer, and the writing layer are usually three different places, and a surface fix almost always leaves two of them intact. I'll come back to this in the deployment-walkthrough pitfalls because it generalizes.
- **The `cron-self-check` yo-yo.** Pre-Phase-7, `cron-self-check` read `expected-crons.json` and re-registered anything "missing" from `oc cron list`. If the file still listed crons that were intentionally retired or moved to host cron — for example the old per-agent heartbeat-check crons that became the host-side fleet-health probe — Mr Fixit would re-create them every midnight UTC, complete with their original cron messages. The symptom was a Telegram alert at 00:01 UTC listing a handful of crons as "re-registered" after you just spent a weekend retiring them. Post-Phase-7, the check has been rewritten to parse `install-host-cron.sh` directly and diff against `crontab -l`. The yo-yo class of bug has been retired along with the `oc cron list` command it depended on, but the lesson — *retirements leave prose fossils* — is still real.

## Write-capable tools

Until 2026-04-15 the fox could only describe failures. He could tell you Huckle Cat's heartbeat was stale; he could not restart the cron. He could tell you a Costco session had expired; he could not trigger the refresh. He was Mr Monitor, not Mr Fixit. The 2026-04-15 patch added three write-capable tools that close that gap without expanding his blast radius beyond the file-and-subprocess level.

| Tool | What it does | Backing | Pending TTL |
|---|---|---|---|
| `propose_rerun_cron(name, reason)` | Re-fires a host cron by name. Looks up the entry in `install-host-cron.sh` (DIRECT or CONTRACT array). Confirm summary always carries an explicit double-execution warning. | `agents/fix-it/_cron_lookup.py` discovers the cron; the confirm executor subprocesses the wrapper directly (DIRECT) or `script-contract-host.sh <logname> <script> <token_env> <timeout>` (CONTRACT). | 2h |
| `propose_snooze_alert(pattern, hours, reason)` | Appends a `match`/`expires`/`reason`/`escalation` block to `KNOWN_ISSUES.md`. The `morning-status.py` classifier picks up the new pattern on its next run and routes matching alerts into the "known" bucket. | Atomic append (tmp + replace) to `~/Dropbox/openclaw-backup/fix-it/KNOWN_ISSUES.md`. Round-trips through `parse_known_issues` for safety. | 1h |
| `propose_refresh_session(source)` | Forces a headless reauth. `source="costco"` subprocesses `agents/shopping/scripts/costco_refresh_headless.py`; `source="google"` calls `google_oauth.refresh_if_stale(token, max_age_days=0)` against both the family-calendar and meetings-coach token files. | `agents/shared/google_oauth.py` for Google; the costco helper module for Costco. | 2h |

The pattern is the one Hilda Hippo's `propose_reorder` already uses. Producer tools (`propose_*`) appear in the LLM tool manifest; their executors stage a pending action and return a `__pending_action__` marker. The dispatcher sees the marker, attaches a Telegram inline keyboard with `[Confirm] [Cancel]` buttons, and the operator's tap routes to a callback-only `confirm_*` executor that runs the actual side-effect. The confirm executors are deliberately not exposed in the manifest — the LLM cannot invoke them directly, only stage actions for the operator to approve.

The Diagnostic Discipline rules (P1–P4 above) apply *before* any propose. The fox is expected to cite evidence — `crontab -l` for a missed schedule, `git log` for a recent commit that might be the cause, `get_fleet_health` for an actually-degraded session — *before* he stages a write. The probation episode happened because he produced a plausible-sounding answer from the wrong evidence; new write tools mean new ways for that failure mode to escalate, so the rule "evidence first, then propose" is structurally re-asserted in `SOUL.md`'s "Write-capable tools" stanza.

A few things are deliberately not in this set. `restart_daemon()` (systemctl --user restart clawford-inbox) is high-risk because it kills every in-flight conversation across the fleet, including the conversation in which the operator would tap the confirm button — an obvious foot-gun. `redeploy_agent()` is high-blast-radius and rare-need: deploying happens by SSH'ing the VPS and running `python3 agents/shared/deploy.py <agent>` by hand, which is more useful as a thing the fox *recommends* than a thing he *does*. The boundary is "things the fox can do without rebooting himself or the fleet." Within that boundary, the three new tools cover the actual recurring fix needs that have come up since the agent went on probation: a missed `costco-token-refresh` cron, a noisy alert that needed a 24-hour suppression while the underlying issue was being fixed, and a Google OAuth token that was about to fall off the back of its 60-day window.

## Deployment walkthrough

The seven-step arc in [Ch 07-0](07-0-your-first-agent.md) applies in full. What follows is the Mr-Fixit-specific material on top.

### Pre-step — The shared brain must exist first

Mr Fixit's job is to validate the shared brain, so the shared brain has to be there for him to read. Walk [Ch 06 — Infra setup](06-infra-setup.md)'s "shared brain" section before deploying him. If the brain isn't set up, the first `brain-validation` cron tick produces a confusing "no such directory" error that looks like an agent bug and isn't.

### Pre-step — Dropbox on the VPS

Mr Fixit reads `~/Dropbox/openclaw-backup/fleet-health.json` and writes to `~/Dropbox/openclaw-backup/agents/fix-it.status.md`. The Dropbox daemon has to be running on the VPS before any of that works. [Ch 06](06-infra-setup.md) covers the headless Dropbox setup story; budget ~1 hour for it the first time, because the fail modes are silent and the defaults assume a desktop user. (The directory keeps the legacy `openclaw-backup` name because renaming it would reset Dropbox sync history fleet-wide. Every agent that reads the brain uses this path.)

### Steps 1-7 from the arc

- **Step 1 (Telegram bot)**: create Mr Fixit's bot via @BotFather. **Important:** Mr Fixit's bot token goes in `~/clawford/.env` as `TELEGRAM_BOT_TOKEN`, not as a per-agent token. He is the fleet's default account — the one that receives the orchestrator's aggregated alerts — and several other crons (`fleet-health-host.sh`, the `cron-self-check` alert path) route their output through this token specifically.
- **Step 2 (bootstrap configs)**: `python3 agents/shared/deploy.py fix-it --bootstrap-configs` scaffolds `SOUL.md`, `IDENTITY.md`, `TOOLS.md`, `AGENTS.md`, `USER.md`, `HEARTBEAT.md`, `MEMORY.md`, `CRONS.md`. Edit each. **Read the Diagnostic Discipline section in `SOUL.md` before you edit it.** If you rewrite it in a way that softens the five rules, you are opting into the exact class of failure that put the agent on probation in the first place.
- **Step 3 (scripts)**: all of Mr Fixit's scripts are already committed. No net-new authoring for a basic deploy. If you are writing a new diagnostic tool for the agent to use, put it in `agents/fix-it/scripts/` and add a `RUN THIS FIRST` entry in `TOOLS.md` the same way `diagnose-approval.py` is documented.
- **Step 4 (manifest.json)**: the example ships with all of Mr Fixit's crons pre-declared. Review `agents/fix-it/manifest.json.example` and copy to `manifest.json`.
- **Step 5 (host-cron registration)**: Mr Fixit's crons are already declared in `ops/scripts/install-host-cron.sh`. No edit needed unless you want to change schedules.
- **Step 6 (deploy)**: SSH, pull, `python3 agents/shared/deploy.py fix-it --yes-updates`. The deploy tool copies the scripts and configs, handles `chattr +i` on `SOUL.md` and `IDENTITY.md`, and writes a backup tarball. **If this is your very first deploy in the fleet**, `deploy.py` will complain that the shared library under `agents/shared/` has to be installed first — that's a Phase 2 prerequisite, covered in [Ch 06](06-infra-setup.md)'s deploy tool section.
- **Step 7 (`install-host-cron.sh`)**: run it on the VPS. Twelve fix-it-related entries should land in `crontab -l`: `fix-it-brain-validation`, `fix-it-conflict-scan`, `fix-it-file-size-monitor`, `fix-it-security-audit-alert`, `fix-it-obsidian-briefing-check`, `fix-it-workspace-snapshot-check`, `fix-it-monthly-archival`, `fix-it-probation-end-reminder`, `fix-it-cron-self-check`, `fleet-health-host`, `morning-status-host`, `morning-fleet-deliver-host`. Count them. The `cron-self-check` cron that runs at midnight UTC will alert if any are missing.

### Smoke test

After deploy and `install-host-cron.sh`, two things to verify:

1. **Fire fleet-health manually and inspect the output:**
   ```bash
   ~/repo/ops/scripts/fleet-health-host.sh
   python3 -c "import json; d=json.load(open('/home/openclaw/Dropbox/openclaw-backup/fleet-health.json')); print(d['generated_at']); [print(f'  {k}: {v[\"status\"]}') for k,v in d['agents'].items()]"
   ```
   `fleet-health.json` should have a fresh `generated_at` and show `fix-it: ok`. If it shows `error` with a `fleet-health.json missing` message, the orchestrator hasn't run yet — re-fire the wrapper and check `~/.clawford/logs/fleet-health-host.log` for the last run's exit code and any traceback.
2. **Fire the morning-status cron manually and confirm the cache file:**
   ```bash
   ~/repo/ops/scripts/morning-status-host.sh
   cat ~/.clawford/fix-it-workspace/cache/morning-brief-ready.txt
   ```
   The cache file should contain a formatted morning brief. If it's empty or missing, `morning-status.py` is failing silently — check its stderr in the host log.

### Bot surface polish

After deploy, set Mr Fixit's slash commands and descriptions via the fleet-wide scripts:

```bash
ssh openclaw@<vps> "bash ~/repo/ops/scripts/set-bot-commands.sh"
ssh openclaw@<vps> "bash ~/repo/ops/scripts/set-bot-descriptions.sh"
```

Both are idempotent and both loop over every bot in the fleet. Run them once immediately after adding a new bot to the scripts, and any time you change the command list.

## Pitfalls you'll hit

> 🧨 **Pitfall.** Rewriting or softening the Diagnostic Discipline section in `SOUL.md` because it reads as overly paranoid. **Why:** the five rules in that section are each a direct codification of a specific failure from the 2026-04-11 incident. If you delete them, you opt into the failure mode they exist to catch — specifically, Mr Fixit will eventually confabulate a diagnosis of an ambiguous symptom under context pressure, and the blast radius will scale with whatever you've given him access to. **How to avoid:** leave the section alone, even if it reads long. If you want to add rules, add them; do not subtract. The probation ledger at `probation.md` is the forcing function that makes this real.

> 🧨 **Pitfall.** Symptom-layer fixes on infra issues that have a reading side and a writing side. **Why:** infra bugs routinely have three layers — the symptom surface (a broken file header, a stale cache entry), a reading side (something consuming the surface), and a writing side (something producing it). A fix at the symptom layer holds until the writing side overwrites it, which is usually within one cron cycle. The canonical example is the 2026-04-14 status-file drift incident: editing the file header fixed the validator, and 22 hours later the `morning-briefing` cron overwrote the fix because its prompt still told the LLM to write the status file. The real fix was in three places: retire the validator's check (reading side), strip "Update your status file" from every cron prompt (writing side), and add the retired phrase to `deploy.py`'s Safeguard 9 forbidden-patterns list as a regression guard. **How to avoid:** every time a surface fix works, ask "what's the reading side and what's the writing side?" If either answer isn't "this was part of the fix too," the bug is going to come back.

> 🧨 **Pitfall.** Retiring a cron or a subsystem without grepping across every cron prompt for the thing you just retired. **Why:** major infra retirements leave prose fossils. R6 migrated the fleet health surface from per-agent `*.status.md` files to `fleet-health.json` weeks before the 2026-04-14 status-file drift incident, but nobody went back and rewrote the cron prompts that told LLMs to keep writing the retired surface. Every one of those prompts was a ticking regression. **How to avoid:** every major retirement ends with three steps — `grep -r "<thing>" agents/*/manifest.json.example agents/*/CRONS.md` to find references in cron prompts, a pass to rewrite each reference to point at the new surface (or delete it), and a regression guard in `deploy.py` Safeguard 9 for the retired phrase. The phrase "Update your status file" is in Safeguard 9's forbidden list because it had to be.

> 🧨 **Pitfall.** Mr Fixit's `cron-self-check` reintroducing crons you just retired. **Why:** pre-Phase-7, `cron-self-check` read `expected-crons.json` and re-registered anything missing from `oc cron list`. If the file still listed retired crons, the check re-created them at midnight UTC, complete with their original prompts. The symptom was a Telegram alert at 00:01 UTC listing a handful of crons as "re-registered" after you spent a weekend retiring them. **How to avoid:** post-Phase-7 this class of bug is structurally retired because `cron-self-check` parses `install-host-cron.sh`'s bash arrays directly instead of reading a separate expected-crons file. But the lesson generalizes: if you have a reconciliation process, its source of truth is the thing that *should* be installed, and that source of truth needs to get updated when you retire something, not separately.

> 🧨 **Pitfall.** Letting Mr Fixit push to GitHub without a pre-push scan. **Why:** Mr Fixit is the only agent in the fleet with `git push` privileges — the other five commit freely to `~/repo/` but only he is trusted to push to the remote. He is also the agent most likely to have a cron scheduled by an LLM that silently writes something it shouldn't (a token, a chat ID, a local path). If that commit goes out without a safety scan, you are rewriting history with `git-filter-repo` before breakfast. **How to avoid:** Mr Fixit's push workflow runs `scripts/pre-push-check.sh` *before* `git push origin master`, and the script hard-fails on tracked secrets, `.env` files, unsuffixed per-agent configs, oversized binaries, and empty commit messages. If the script finds something, he alerts the human on Telegram and refuses to push. I've had that refusal fire twice. Both times I was grateful.

> 🧨 **Pitfall.** Invoking Claude Code via a persistent session instead of a one-shot shell call. **Why:** Mr Fixit can invoke Claude Code as a repair tool for complex diagnostics, and there are two ways to do that — a one-shot shell command (`claude -p ... --add-dir ~/Dropbox/openclaw-backup/`) and a persistent-session protocol. Persistent sessions bind to the Telegram thread they started in, and once that binding is active, every subsequent message to the fox's chat routes to Claude Code instead of to Mr Fixit. The fox's Telegram channel becomes a Claude Code session you cannot exit. **How to avoid:** use `claude -p` as a one-shot shell command, always with `--add-dir ~/Dropbox/openclaw-backup/`, and always with a fresh `--session-id \$(uuidgen)` on the first turn. Mr Fixit's `SOUL.md` has this rule explicit; leave it there. The full story is in the [Ballad of Mr Fixit](../docs/ballad-of-mr-fixit.md), act III.

## See also

- [Ch 02 — What Clawford Isn't](02-what-clawford-isnt.md) — the liberation's decision doc, and the historical context for why Mr Fixit's current architecture looks the way it does.
- [Ch 06 — Infra setup](06-infra-setup.md) — `deploy.py`'s safeguards, the shared library, the shared brain that Mr Fixit validates, the host-cron runtime.
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the script contract, the canonical deploy path, the LLM-vs-deterministic line.
- [Ch 07-0 — Your first agent](07-0-your-first-agent.md) — the general seven-step deploy arc Mr Fixit inherits from.
- [Ch 08 — Security and hardening](08-security-and-hardening.md) — the `chattr +i` immutable-identity story and the defense-in-depth posture.
- [`agents/fix-it/SOUL.md.example`](../agents/fix-it/SOUL.md.example) — the full values and boundaries document, including the Diagnostic Discipline section that exists because of the probation incident, and the "Write-capable tools" stanza that gates `propose_rerun_cron` / `propose_snooze_alert` / `propose_refresh_session` behind the same diagnostic rules.
- [`agents/fix-it/tools.py`](../agents/fix-it/tools.py) and [`agents/fix-it/_cron_lookup.py`](../agents/fix-it/_cron_lookup.py) — the tool manifest plus the helper that discovers crons by name in `install-host-cron.sh`.
- [`agents/fix-it/retire.sh`](../agents/fix-it/retire.sh) — the pre-baked retirement script. Read it once even if you never need to run it; knowing exactly what the consequence path does is part of the calibration.
- [`ops/scripts/fleet-health-host.sh`](../ops/scripts/fleet-health-host.sh), [`morning-status-host.sh`](../ops/scripts/morning-status-host.sh), [`morning-fleet-deliver-host.sh`](../ops/scripts/morning-fleet-deliver-host.sh) — the three host-side wrappers that make up Mr Fixit's fleet-observability surface.
- [`DEPLOY.md`](../DEPLOY.md) — the nine `deploy.py` safeguards with outage stories, plus the historical Gotchas appendix for pre-liberation failures.
- [`docs/ballad-of-mr-fixit.md`](../docs/ballad-of-mr-fixit.md) — the five-act tragedy. Required reading at least once. Ideally before you deploy the fox rather than after.
