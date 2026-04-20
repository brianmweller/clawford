# Auth architectures

*Last updated: 2026-04-17 · Reading time: ~15 min · Difficulty: hard*

> **TL;DR.** Six distinct auth shapes show up across the six-agent Clawford fleet, and most new agents will reuse one of them rather than invent a seventh. This chapter names the shapes, lists which agents use which, documents the three cross-cutting idioms (local-then-SCP token distribution, gitignored credential files in the workspace `cache/`, and a hard ban on raw API keys in cron-invoked scripts), and collects the pitfalls that repeat across more than one agent. If you are deploying a brand-new agent and the question is *"what auth should this talk to?"* — start here, pick the shape, then read the agent chapter that most resembles the new agent.

## The three cross-cutting idioms

These three rules apply to every auth shape in the fleet. They are not optional.

**Idiom 1 — Local-then-SCP for browser-based auth.** Any auth flow that needs a human to click through a consent screen or scan a QR code runs on the operator's local machine, produces a token file (or a profile directory), and gets SCPed to the VPS after the fact. The VPS never runs a browser. This pattern is used by every OAuth shape (Shape 1, Shape 2), every Camoufox-based auth (Shape 4, Shape 5), and every QR-code pairing (Shape 6). The reason is simple: headless browser automation is fragile, `ssh -X` is slow and sometimes broken, and manual URL copy-paste flows are error-prone. Running the flow locally against a real browser sidesteps all three failure modes at a cost of one `scp` command.

**Idiom 2 — Credentials live in `cache/` under the workspace, gitignored.** Every token file, session directory, cookie jar, and persistent browser profile lives under `~/.clawford/{agent}-workspace/cache/` (or a sibling directory under the workspace root), and the workspace `.gitignore` lists every credential filename explicitly. Nothing credential-shaped ever touches `git add`. The operator's local copy and the VPS copy both live outside the repo, synced via `scp`, never via `git pull`. If you see a token file path that starts with `agents/{agent}/` rather than `~/.clawford/{agent}-workspace/`, treat it as a bug.

**Idiom 3 — No raw API keys in cron-invoked scripts.** LLM calls go through `agents.shared.llm.infer` (which uses the operator's ChatGPT Plus subscription via the `codex` CLI — see [Ch 04 — VPS setup](04-vps-setup.md) and [Ch 06 — Infra setup](06-infra-setup.md)). API keys for third-party services that genuinely need them (Telegram bot token, Workflowy bearer token, transcription vendor OAuth client id) live in a workspace-local `.env` file that is gitignored. The `.env` file is loaded by the script on startup and never logged, never printed to stdout, and never passed as a CLI argument. If a new script wants an API key, the default answer is "no, route it through `agents.shared.llm.infer` instead," and the exception is "this is a non-LLM third-party service and the key is in `.env`."

## The six auth shapes

### Shape 1 — OAuth 2.0 with long-lived refresh token

**Used by:** [Mistress Mouse](12-mistress-mouse.md) (Google Calendar + Gmail), [Sergeant Murphy](13-sergeant-murphy.md) (Google Calendar + Gmail), [Huckle Cat](14-huckle-cat.md) (Google Calendar + Gmail + Google Contacts).

**Token lifetime:** refresh tokens effectively never expire for personal Google accounts. The only things that invalidate them are explicit user revocation, project deletion, or removal from the Google Cloud OAuth consent screen test-users list. None of those happen by accident.

**Setup flow:**
1. Create a Google Cloud project, enable the relevant APIs (Calendar, Gmail, Contacts).
2. Create an OAuth 2.0 Desktop Client ID in the Credentials page. Download `credentials.json`.
3. Add the operator's email to the OAuth consent screen's Test users list. **This is the step that is easy to forget.** Without it, the first consent attempt returns `Access blocked: {app name} has not completed the Google verification process`, and the error message buries the lede.
4. Run `google-auth-setup.py` (or the per-agent equivalent) locally. `InstalledAppFlow.run_local_server(port=8080, open_browser=True)` pops a browser, runs the consent flow, writes `token.json`.
5. SCP both `credentials.json` and `token.json` to `~/.clawford/{agent}-workspace/`.
6. Verify with a one-shot read call (`gcal-fetch.py --days 1`) on the VPS.

**Scope changes require re-auth.** If the agent later needs a bigger scope (e.g., upgrading from `calendar.readonly` to `calendar` full write), re-run the auth flow locally with the new scope list. A new consent screen pops, a new `token.json` is written, re-SCP to the VPS.

**Canonical reference:** [§ The Google OAuth pattern in Ch 12](12-mistress-mouse.md#the-google-oauth-pattern).

### Shape 2 — OAuth 2.1 with rotation-prone refresh token

**Used by:** [Sergeant Murphy](13-sergeant-murphy.md) (the MCP transcription provider).

**Token lifetime:** refresh tokens can get invalidated on the order of months — long enough to forget about, short enough to be surprising. Unlike Google's refresh tokens (Shape 1), these do get rotated, and rotation can happen at vendor discretion (security-event revocations, policy changes, session cleanups). The agent's soul-level boundary is explicit: **never authenticate automatically.** On a 401, the agent alerts the operator (rate-limited to one alert per 90 minutes) and waits for manual re-auth.

**Setup flow:** same local-then-SCP shape as Shape 1 — run `transcript-auth-manual.py` locally, complete the OAuth 2.1 consent, SCP the resulting token file to the VPS.

**The 401 recovery flow.** When the transcription vendor invalidates the refresh token, every cron that touches the provider gets a 401 on its next run. The first 401 writes `cache/transcript-last-alert.json` with a timestamp and sends one Telegram alert. Subsequent 401s within 90 minutes check the file and silently skip the alert. The operator re-runs the auth script locally, SCPs the new token, and the next cron tick recovers automatically. See [§ The MCP transcription integration in Ch 13](13-sergeant-murphy.md#the-mcp-transcription-integration).

### Shape 3 — Bearer token in `.env`

**Used by:** [Sergeant Murphy](13-sergeant-murphy.md) + [Huckle Cat](14-huckle-cat.md) (Workflowy), every agent that sends Telegram messages (Telegram bot token). Any Tier 3 integration using a residential-proxy service also stores credentials this way.

**Token lifetime:** varies by vendor. Workflowy bearer tokens are long-lived and only rotate on explicit user action. Telegram bot tokens are effectively permanent. Residential-proxy credentials rotate on a manual cadence.

**Setup flow:** generate the token from the vendor's developer settings, paste it into the workspace `.env` file, verify with a ping script.

**The security boundary.** The `.env` file lives in `~/.clawford/{agent}-workspace/.env` and is gitignored. The file is loaded by a shared `agents.shared.env.load_env` helper that reads it into the process environment before any script logic runs. Scripts reference the values via `os.environ.get("WORKFLOWY_TOKEN")` — they never hardcode the value, never print it, and never pass it as a CLI argument. If a new script needs a `.env` value that is not yet defined, add it to `.env.example` (the tracked template) with a placeholder value, and document it in the agent's `TOOLS.md`.

### Shape 4 — Persistent browser profile (Playwright)

**Used by:** no public consumer at present. The pattern is stable and documented here for anyone adding a new integration whose target is a normal web app (not a clean API, not a hostile retailer).

**Session lifetime:** days to weeks, dependent on the vendor's idle-session cleanup. Long-idle sessions tend to expire after a day or two, which is why a `*-keepalive` cron that fires a lightweight page-load every few hours is often necessary to keep the session from going idle.

**Setup flow:**
1. Run a vendor-specific `*-auth.py` on the operator's local machine. A Chromium window opens against the vendor's login page.
2. The operator logs in by hand, including any 2FA the vendor requires.
3. Playwright writes the session state (cookies + local storage) to a persistent profile directory.
4. SCP the entire profile directory to the VPS workspace.

**Why Playwright and not Camoufox here.** Shape 4 targets are vendors whose bot detection is aggressive on login pages but lenient on authenticated page-loads. A plain Chromium profile, once authenticated, is indistinguishable from a real user for the page-load cadence these targets use. Camoufox (Shape 5) is the tool for surfaces where even the authenticated traffic looks bot-shaped without heavy anti-fingerprinting.

### Shape 5 — Camoufox + residential proxy + auto-MFA

**Used by:** no public consumer at present. The shape is the Tier 3 pattern documented in detail at [Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice); the shared library (`camoufox_proxy.py`, `retry_policy.py`) ships the scaffolding. Anyone building an integration against a hostile-vendor surface (Azure B2C + Akamai, or similar) lands here.

**Session lifetime:** varies by vendor. A typical profile: Azure B2C custom policy with a PKCE public-client flow that a daemon keeps alive in the background via refresh-token rotation, falling back to browser silent refresh when the RT chain ends. Some vendors' consumer logins expire and require re-login via an auto-MFA flow that reads a TOTP secret from `secrets.env`.

**Setup flow:** this is the heaviest auth shape in the fleet. Four dependencies compose:

1. **Camoufox** — hardened Firefox with anti-fingerprinting patches. Installed via `pip install camoufox` + `camoufox fetch` for the underlying binary. Used when plain Playwright + Chromium is too easy to fingerprint.
2. **Residential egress** — either a sticky-port residential-proxy subscription, or (strongly preferred when available) a SOCKS5 tunnel over Tailscale to a machine on the operator's home network. See [Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice) for why the home-ISP path collapses the adversarial game entirely.
3. **TOTP secret in `secrets.env`** — the 2FA seed for the vendor account, stored in a dedicated `secrets.env` (chmod 600, separate from regular `.env`, sourced as a second env_file). The auto-MFA script reads the seed on each login attempt, generates the 6-digit code via `pyotp`, and submits it to the MFA field automatically.
4. **Persistent Camoufox profile** — cookies + local storage, stored under `~/.clawford/{agent}-workspace/camoufox-profile/{vendor}/`.

**The auto-MFA rule.** Never ask the operator to manually re-authenticate a Shape 5 vendor. Every login flow must be fully automated, including the MFA step. The operator-time cost of a manual re-auth ("can you open your phone and read me the 6-digit code right now") is high enough that the correct engineering answer is "build the auto-MFA once and never ask again."

**Setup cost.** Shape 5 is expensive to set up the first time — expect one to two weeks of evenings for the first vendor. Re-use the `agents/shared/camoufox_proxy.py` + `retry_policy.py` scaffolding for any new vendor that lands in this shape. The Tier 3 patterns in Ch 06 name the specific traps.

### Shape 6 — QR-code pairing

**Used by:** [Huckle Cat](14-huckle-cat.md) (Google Messages Web).

**Session lifetime:** weeks to months. Google Messages Web uses a "linked device" pairing model — the operator scans a QR code once, and the session persists as long as the vendor's device-link cleanup doesn't flag it.

**Setup flow:**
1. Run the pairing script on the operator's local machine. It opens a Camoufox browser against the vendor's linked-device page.
2. The page displays a QR code. The operator scans the code with their phone.
3. The browser's persistent profile captures the session cookies + local storage.
4. SCP the entire profile directory to the VPS workspace.

**The screenshot-QR variant.** [Huckle Cat's `gmessages-auth.py`](14-huckle-cat.md#the-google-messages-devtools-story) uses a screenshot-QR variant where the browser takes a screenshot of the QR code at a known DOM coordinate and saves it to a local file for the operator to scan with their phone. This avoids the need for a debug-port flow.

**The liability footnote.** Shape 6 is the most fragile of the six shapes. Vendor terms of service typically forbid reverse-engineered linked-device clients, and a ban on the bound account is a real risk. Deploy Shape 6 integrations only when the value clearly outweighs the ban risk, and never share a Shape 6 session across multiple agents.

## Per-agent reference table

| Agent | Shape 1 | Shape 2 | Shape 3 | Shape 4 | Shape 5 | Shape 6 |
|-------|---------|---------|---------|---------|---------|---------|
| [Mr Fixit 🦊🔧](09-mr-fixit.md) | — | — | Telegram | — | — | — |
| [Lowly Worm 🐛📰](10-lowly-worm-newsfeed.md) | — | — | Telegram | — | — | — |
| [Mistress Mouse 🐭📅](12-mistress-mouse.md) | Google Cal + Gmail | — | Telegram | — | — | — |
| [Sergeant Murphy 🐷🔍](13-sergeant-murphy.md) | Google Cal + Gmail | Transcription provider | Telegram + Workflowy | — | — | — |
| [Huckle Cat 🐱🤝](14-huckle-cat.md) | Google Cal + Gmail + Contacts | — | Telegram + Workflowy | — | — | Google Messages |

A few observations from the table:

- **Shape 3 is universal.** Every agent that sends a Telegram message uses Shape 3 for the bot token. This is the cheapest auth shape in the fleet and the default for any new vendor that hands you an API key.
- **Shape 1 is the Google-cluster shape.** Three agents touch Google services, and all three use the same Shape 1 pattern against a single Google Cloud project. Reuse the project across agents — one Google Cloud project, one OAuth Desktop client, different scopes per agent.
- **Shapes 4 and 5 have no current consumer in the public fleet.** They are documented here as stable patterns for anyone adding a new integration. Shape 5 in particular is weeks of work the first time — read [Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice) before starting.
- **Shapes 2 and 6 each have one or two consumers.** These are the custom shapes. When you add a new agent and it needs one of these, re-read the single reference chapter for that shape before starting.

## Pitfalls

> 🧨 **Pitfall.** Running any browser-based auth flow on the VPS. **Why:** the VPS is headless. `InstalledAppFlow.run_local_server(port=8080, open_browser=True)`, Camoufox, Playwright, and QR-code pairing all need a real browser. Every attempt to run them over `ssh` hangs, fails, or leaves half-authorized state behind. **How to avoid:** every auth flow in every shape runs on the operator's local machine, and the resulting token file (or profile directory) gets SCPed to the VPS. Idiom 1 above is load-bearing, not a suggestion.

> 🧨 **Pitfall.** Committing a token file or a `.env` value to git. **Why:** once a credential is in git history, the cheapest recovery is to rotate the credential at the vendor. Vendor-side rotation is slow, intrusive, and sometimes impossible for shape 6 / shape 4 setups (you would have to re-pair or re-authenticate by hand). A token leaked to a public repo is also a hard "rotate now" incident. **How to avoid:** every workspace has a `.gitignore` that lists `token.json`, `credentials.json`, `.env`, `cache/`, `camoufox-profile/`, and any other credential-shaped filename the agent uses. Idiom 2 is the default; if you are adding a new credential file, add its path to `.gitignore` in the same commit that creates the file.

> 🧨 **Pitfall.** Using raw API keys for LLM calls. **Why:** raw OpenAI / Anthropic API keys in scripts mean a parallel billing surface the operator has to track separately from the main LLM subscription. For a personal Clawford fleet, the correct answer is to route every LLM call through `agents.shared.llm.infer`, which uses the operator's ChatGPT Plus subscription via the `codex` CLI — no API key, no parallel bill, no token to rotate. **How to avoid:** never add `OPENAI_API_KEY` or equivalent to any `.env` file. If a new script wants to call an LLM, the only sanctioned path is `from agents.shared import llm; llm.infer(...)`. The one-time mining pipeline for [Huckle Cat](14-huckle-cat.md#the-mining-pipeline) is an explicit exception because it runs locally, not as a VPS cron, and the cost is visible in the local bill during the run.

> 🧨 **Pitfall.** Skipping test-user registration on the OAuth consent screen. **Why:** Shape 1 (Google OAuth) requires the operator's email to be on the consent screen's Test users list. Skipping this step returns `Access blocked: {app name} has not completed the Google verification process` on the first consent attempt, and the error looks like a permissions problem with the API scope rather than a "your email is not on the test-users list" problem. **How to avoid:** every new Shape 1 deploy has a pre-step: go to the Google Cloud OAuth consent screen, scroll to Test users, add the operator's email, save. Only then run the auth flow. If the error shows up anyway, the fix is the test-users list, not the API scopes.

> 🧨 **Pitfall.** Treating a 401 from a Shape 2 vendor as retryable. **Why:** Shape 2 vendors can invalidate refresh tokens outside the agent's control. A 401 means the refresh flow no longer works, period. Retrying on a hot loop burns alerts and cron budget and does not help. **How to avoid:** every Shape 2 integration has a rate-limited alert pattern (one alert per 90 minutes) and explicit manual-re-auth recovery. See [§ The MCP transcription integration in Ch 13](13-sergeant-murphy.md#the-mcp-transcription-integration) for the canonical pattern.

> 🧨 **Pitfall.** Asking the operator to manually re-auth a Shape 5 vendor. **Why:** the operator-time cost of manual re-auth ("open your phone, read me the 6-digit code") is high enough that the correct engineering answer is to build auto-MFA once and never ask again. Any Shape 5 integration that falls back to manual re-auth is one step from being abandoned. **How to avoid:** the first-deploy budget for a Shape 5 vendor includes the auto-MFA path. The TOTP secret lives in `.env`, the login script reads it, generates the 6-digit code via `pyotp`, and submits it to the MFA field automatically. No shortcuts.

> 🧨 **Pitfall.** Sharing a Shape 6 session across multiple agents. **Why:** Shape 6 is already on the "ban-risk" side of the vendor's terms of service. A single bound account with two simultaneous linked-device sessions reading the same conversation stream is exactly the pattern vendor detection is looking for. **How to avoid:** one bound account per Shape 6 agent. If two agents need to read the same linked-device channel, use two different accounts, or disable one of the two integrations.

> 🧨 **Pitfall.** Forgetting that SCP is the sync path, not `git pull`. **Why:** the credential files live outside git by design (Idiom 2). A new operator cloning the repo and running `git pull` on the VPS will have no token files at all, and every OAuth-based cron will fail on the first tick. **How to avoid:** the deployment walkthrough for every agent calls out the SCP steps explicitly. After `git pull`, verify that the workspace has the expected token files with a one-shot read call. If the read call fails, the SCP step was skipped, not the code.

> 🧨 **Pitfall.** Cross-workspace symlinks for a shared token file. **Why:** the historical convenience pattern was `{agent-a}-workspace/token.json → ../{agent-b}-workspace/token.json` so two agents share one Google OAuth token. That works fine until the [P1.2 bubblewrap layer](19-security-and-hardening.md#defense-layer-7-process-level-isolation-p12) lands — each agent's bwrap namespace only binds its own workspace, so the symlink resolves to a path that isn't visible inside the namespace and the agent's cron errors with "token.json not found". The regression took a morning digest dark for a day. **How to avoid:** every credential file must be a real file inside the consuming agent's workspace. If two agents need the same Google OAuth token, SCP the token to BOTH workspaces (or use the deploy tool's auto-heal — `heal_cross_workspace_symlinks` runs every deploy and resolves any offending symlink into an independent copy). Each agent then refreshes its own token on its own schedule — the refresh_token is typically long-lived enough that parallel refresh doesn't cause drift.

## See also

- [Ch 04 — VPS setup](04-vps-setup.md) — the `codex` CLI setup that underpins the no-raw-API-keys rule for LLM calls
- [Ch 06 — Infra setup](06-infra-setup.md) — the shared library layout that hosts `agents.shared.llm.infer` and `agents.shared.env.load_env`
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the deploy path and safeguard story
- [Ch 12 — Mistress Mouse 🐭📅](12-mistress-mouse.md) — canonical Shape 1 (Google OAuth) chapter
- [Ch 13 — Sergeant Murphy 🐷🔍](13-sergeant-murphy.md) — canonical Shape 2 (rotation-prone OAuth)
- [Ch 14 — Huckle Cat 🐱🤝](14-huckle-cat.md) — canonical Shape 6 (screenshot-QR variant)
- [Ch 19 — Security and hardening](19-security-and-hardening.md) — the credential-storage + attack-surface story
