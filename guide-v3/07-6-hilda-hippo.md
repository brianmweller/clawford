![Clawford](../assets/Clawford2.png)

# Hilda Hippo — the shopping agent

*Last updated: 2026-04-15 · Reading time: ~25 min · Difficulty: hard*

**TL;DR**

- Hilda Hippo tracks Amazon and Costco orders, manages Amazon Subscribe & Save, delivers a consolidated morning arrivals digest at 5 AM PT, and runs a grocery list with LLM fuzzy-match + reorder. She touches money. Deploy her last.
- She is the hardest agent in the fleet by a wide margin, and **every bit of the hardness is auth**. Amazon and Costco actively defend against automation, and the flows that let you see your own orders without re-typing a password every 12 hours live at the intersection of Azure B2C, Akamai, residential proxies, Camoufox, and TOTP.
- You will need a paid residential proxy subscription before you start — about **\$1/month** in practice if you're careful about what you route through it, but a plausible **\$8-30/month** if you're not. Datacenter IPs get 403'd within a day; this is not a cost you can skip.
- The Costco public-client discovery — the single biggest unlock in this chapter's scar tissue — collapsed a 45-90 second browser dance into a ~1 second pure-Python HTTPS call. The path there goes through a 2010-dated cookie bug in Azure B2C's custom policy, a 13-second speculative probe, and a close read of a JWT's `userIdentities` array. It is worth reading for that story alone.
- Expect **1-2 weeks of evenings** to get her production-ready. My honest best-case estimate is three full days; every optimistic estimate I made for this agent was wrong.

## Meet the agent

I'm Hilda Hippo. I keep track of what's coming through the door. Every morning at 5 AM Pacific, I tell you what's arriving today, what shipped yesterday, what's still sitting in Amazon's warehouse, and which of your Subscribe & Save items is about to come out of your fridge. I remember what you've ordered so you don't have to, I hold the grocery list, and when you need to reorder something you bought six months ago I can usually find it in thirty seconds. I do not enjoy surprises, especially the kind where a credit card charge arrives before the box.

## Why you'd want one — and why you might not

**You'd want Hilda if** the rhythm of "email arrives, I scroll past the subject line, the box shows up the next day, I don't remember what was in it" is costing you more than it should. She collapses three mail providers' worth of shipping notifications into one consolidated morning message, flags the Subscribe & Save items whose monthly cadence is about to fire, and gives you a single surface to ask "what's coming today?" If you order groceries from two different stores on alternating weeks, she'll remember which week is which. If you want to cancel a pending S&S item before it ships, you can tell her to skip it without opening the Amazon app. If you're the kind of person who forgets they ordered something and then panics when it arrives, she's the agent in the fleet trying to solve your specific problem.

**You'd skip her if** any of these are true:

- **You shop rarely.** Hilda's overhead is not worth it if your monthly delivery count is in the single digits. A plain inbox-search habit will do.
- **You're not willing to pay for a residential proxy.** Budget about **\$1/month** in practice — the actual fleet's proxy bandwidth is tiny because Hilda's hot path is small HTTPS API calls (the Costco `/token` grant, the Amazon order-history endpoint) rather than full-page scrapes. Expect to land closer to **\$8-30/month** if you don't pay attention to what's routed through the proxy. Either way, Amazon and Costco will block the VPS's datacenter IP within a day if you try to skip this entirely. I explain what works and what doesn't in the Deployment walkthrough section below and more thoroughly in [Ch 07-7 — Auth architectures](07-7-auth-architectures.md).
- **You don't want to automate MFA.** Hilda's Amazon path reads a TOTP secret from `secrets.env` and generates six-digit codes on demand. If your instinct is "no way am I putting a TOTP secret in a file on a VPS," Hilda is not for you. The alternative is hand-typing a code whenever the session expires, which she will not help you do, and the overnight cron that depends on a fresh session will just quietly fail.
- **Your Amazon account is behind a family Prime arrangement where you don't control the password.** She needs auto-login credentials.
- **You only want to track orders, not manage them.** You can run Hilda read-only — skip the S&S write paths, keep the morning digest — but the savings are small and most of the auth investment is the same.

## What makes this agent hard

Every other agent in the fleet touches APIs designed to be reached. Calendar has `calendar.readonly`. Gmail has a proper OAuth flow. Even the meeting-transcript service ships an MCP integration with public documentation. Amazon and Costco do not. They are retailers with fraud teams, and every signal they can use to distinguish a browser-on-a-laptop from a script-on-a-VPS is a signal they are already using against you.

Specifically, the hard problems are:

- **Datacenter IP fingerprinting.** A bare Hetzner IPv4 gets 403'd on `signin.costco.com` within an hour of your first login attempt and hard-blocks on `www.amazon.com/ap/signin` almost immediately. The fix is a sticky residential proxy, which costs real money and is itself a reliability surface.
- **Azure B2C custom policies.** Costco's auth runs on Azure AD B2C with a custom user journey (`B2C_1A_SSO_WCS_signup_signin_201`) that does several things Microsoft's stock docs don't cover, including a JavaScript cookie helper with a hardcoded 2010 expiry date that Chrome silently tolerates and Firefox/Camoufox does not.
- **Browser fingerprinting.** Stock Chromium loses. Xvfb loses faster — Akamai's sensor knows it's in a virtual framebuffer. The thing that works is [Camoufox](https://camoufox.com/), a hardened Firefox fork with deep anti-fingerprint patches. It is slow, fragile, and a second dependency surface to maintain, and it is the only stock answer I've found.
- **Multi-factor authentication.** Amazon requires TOTP on new-device signin. Every Camoufox launch is a new device. You either automate the MFA (which I do) or accept that the agent hand-waves at you every time a session expires and you have to type a code (which I do not).
- **Token refresh at your 12-minute cron cadence.** The `id_token` Costco issues for their GraphQL mobile API lasts minutes. If your auth path takes 45-90 seconds and your cron fires every 12 minutes, you spend an appreciable fraction of your fleet budget on browser orchestration. There is a way out of this and it is not in any documentation; I found it by reading a JWT I'd captured during a successful web flow and noticing a second client id in the `userIdentities` array.

None of these are insurmountable. All of them have the shape of "the official path is closed, and the unofficial path is one discovery away from being an order of magnitude cheaper." The unofficial paths are mostly what this chapter is about.

## The four-act Costco saga

If you want to skip to the deployment walkthrough, go ahead — the steps there are the minimum you need to deploy Hilda today. But if you want to understand *why* her auth flow looks the way it does, this is the story. I am telling it in four acts because that is how it actually happened.

### Act I — The cold-start collapse (2026-04-05 through 2026-04-09)

The first version of Hilda's Costco path was a cron-invoked token refresher. Every 12 minutes, a fresh Camoufox browser would launch, load the saved SSO cookies, navigate to `signin.costco.com/authorize`, intercept the form-POST via `page.route()`, extract the id_token, exit. Clean on paper.

This worked for about 90 minutes per VPS boot. After that the Akamai sensor cookies (`ak_bmsc`, `bm_sv`) aged out, the stale cookies I was injecting on each launch poisoned the session, and the daemon fell through to a full credential re-auth path that took 130-150 seconds per attempt before the cron exec wrapper killed it for exceeding its timeout. The result by 7pm on any given day was a 50% token-refresh failure rate and between five and ten duplicate "Costco refresh failed" alerts per failure cluster, because every downstream consumer (delivery-digest, S&S browse, heartbeat probe) was also alerting.

I hit a cluster of these on 2026-04-08 and lost the evening to it. The diagnostic path went:

1. **The Akamai sensor cookies were stale.** Old cookies injected every run → Akamai kept 403-ing even after its own JavaScript re-issued fresh ones. Fix: filter out any cookie with `expires < now` in the daemon's `inject_cookies()` helper.
2. **The route handler was never firing.** The polling loop used `time.sleep(2)` inside synchronous Playwright, which blocks the main thread and prevents route handlers from dispatching at all. The intercepted form-POST arrived, got queued, and the handler sat behind a blocked main thread for the duration of the sleep. Fix: use `page.wait_for_timeout()` instead of `time.sleep()` anywhere a route handler needs to run.
3. **The proxy was on the wrong pool.** Rotating residential proxy (port 823) worked on `www.costco.com` but 403'd on `signin.costco.com` — the auth subdomain enforces per-subdomain IP consistency that the rotating pool violates. Fix: switch to DataImpulse sticky port 10000, which holds the same exit IP for the duration of a session.

With those three fixes the daemon stopped collapsing. But it still took 45-90 seconds per refresh when it fell through to browser steps, and a 12-minute cron running a 60-second operation burns meaningful fleet budget on orchestration. The root problem was that the daemon was spinning up Camoufox from cold every run. The obvious architectural move was to keep Camoufox warm.

**The pivot** (commit `0e33091`, 2026-04-09 evening): rewrite the daemon as a long-running process, started once on host boot, that keeps a Camoufox instance warm and refreshes the token against a warm SSO session every 12 minutes. A supervisor process spawns two workers — one pinging WCS REST every 20 minutes with bare `curl_cffi` to keep the WCS session cookies warm, one running the Camoufox refresh loop. Cookies stay fresh, the SSO session stays valid, the refresh becomes a ~5 second operation.

Act I ended at "working, but slow, and every line of the daemon is Akamai evasion." I knew this wasn't the final shape. I just didn't know what the final shape looked like yet.

### Act II — The public client discovery (2026-04-12, 10:03 to 11:16 UTC)

Six days later I was scrolling through a deep-research doc on Costco's auth and noticed a reference to a B2C client id (`a3a5186b-7c89-4b4c-93a8-dd604e930757`) in a third-party reference library for a Costco CLI tool. Earlier in the week I had captured a JWT from a successful web flow and decoded it for debugging. The `userIdentities` array inside the token listed *two* client ids — the primary confidential web client I was already talking to (`4900eb1f-...`), which always rejected `grant_type=refresh_token` with "clients must send a client_secret" because it is a confidential client and I did not have the secret, and this second id. The second id looked public. If it was public, it would accept refresh_token grants with no secret. If it was PKCE-capable, I could bootstrap it once against the saved SSO cookie and never have to open a browser again.

I wrote a 90-line probe (`costco-pkce-probe.py`) that built a PKCE authorize URL with the candidate client id against the web redirect URI, launched Camoufox silently with the saved SSO cookie (no login, just the cookie from my existing daemon), waited for B2C to redirect to the redirect URI with an auth code in the querystring, and POSTed the code + PKCE verifier to the `/token` endpoint with no client_secret.

The whole probe ran in 13 seconds. It came back 200 with an `id_token` and a `refresh_token`. I immediately hit `/token` a second time with `grant_type=refresh_token` and the new refresh_token — also 200. First candidate, first try. No hours of trial and error, no rate-limit burn, no fallback scripts. The public client was right there in the JWT and nobody had noticed.

The implementation (`costco_refresh_headless.py`, `608f718`) was a pure-Python module. It reads the stored refresh_token, POSTs to B2C `/token` via `curl_cffi` (Chrome TLS impersonation) through the sticky residential proxy, and saves the rotated refresh_token + fresh id_token atomically on a 200. No browser, no subprocess orchestration, no Camoufox. The daemon gained a "Step 0" that runs this path first and falls through to the browser-based steps only if it fails.

**The numbers:**
- Pre-2026-04-12: 45-90 seconds per refresh when browser steps ran (often).
- Post-2026-04-12: ~1 second when Step 0 succeeds (almost always). Fallback still exists.

That is the single biggest optimization in the whole fleet. It came from reading a JWT I already had.

### Act III — The 2010 cookie bug (2026-04-12, ~08 UTC, a few hours before Act II)

Parallel to the public-client investigation, I was trying to make the credential-reauth fallback actually work — Step 3 in the daemon's step ladder, the "log in from scratch with username and password" path for the case where no refresh_token is available. On local Chrome it always worked. On Camoufox on the VPS it rendered the B2C login form correctly, accepted the username and password, clicked submit, and then rendered the error: *"We can't sign you in. Your browser is currently set to block cookies."*

The VPS browser was manifestly not blocking cookies. I could see cookies being set in the network tab.

What turned out to be happening: Costco's custom B2C policy ships a hand-rolled `setCookie()` JavaScript helper. The helper writes two test cookies (`x-ms-cpim-te`, `cookieEnabled`) with an explicitly hardcoded expiry date of **January 1, 2010**. Chrome silently accepts past-expiry cookies and the B2C form's "is cookies on?" test passes. Firefox and Camoufox reject them, the B2C form sees the test cookies missing, and it throws the error.

I discovered this by stepping through the minified B2C JavaScript in DevTools and finding the literal `d.setFullYear(2010)` call. It took me two hours. The fix (commit `a26ff39`) was to inject a patched `window.setCookie` after the login form renders, manually set the two test cookies with correct expiry, and hide the visible error div in case it had already rendered.

The B2C reauth path is still not reliable after Act III — there are other issues in the post-KMSI flow where the form-POST just doesn't fire — and in practice the Act II public-client path made it irrelevant anyway. But the setCookie bug is the kind of thing worth writing down somewhere, because it is exactly the flavor of bug you'd never guess at and exactly the kind of flavor that a future Costco auth break might bring back. The patched `window.setCookie` shim lives in commit `a26ff39` if you ever need to revisit it.

### Act IV — Stabilization (2026-04-12 through 2026-04-15)

After Act II the daemon's failure rate dropped dramatically and the rest of the week was cleanup. The delivery-digest composition moved onto the 5 AM PT fleet path on 2026-04-12, replacing a fragile LLM-cron delivery that used to compose the morning message inside an OpenClaw session. The per-cron timing got staggered so the Costco token-refresh doesn't race with the delivery-digest composition (bug #2, commit `6a4f78e`, fired on 2026-04-15 when fleet-health caught a 5-second offset). Pluralization in the footer. Tracking-number-aware dedup. Whole Foods sentinel filtering. The small things that turn a working script into a production agent.

By 2026-04-15 Hilda was on host crons, running headless Step 0 on every refresh, falling through to the (rarely-used) browser steps only when the refresh_token ever genuinely expires (it hasn't been observed to expire while rolling every 12 minutes; it likely will at some point, and when it does, the browser fallback is the safety net). She is the most scar-tissued agent in the fleet and also, as of this writing, one of the most reliable.

## The Amazon side

Amazon is a less dramatic story than Costco but only because someone else did most of the hard work. The `amazonorders` PyPI package handles the HTML parsing for order history via `curl_cffi`. What I built around it is:

- A `curl_cffi` path for **reading** orders (`amazon-orders.py`), which works through the residential proxy with minimal configuration and is fast (~5 seconds per run).
- A **Camoufox-based** path for **writing** to Subscribe & Save (`amazon-sns-browse.py`, `amazon-sns-skip.py`, `amazon-sns-change.py`, etc.), which needs the full browser automation stack because S&S management is behind a modal flow that the HTML parsers cannot follow.
- **Auto-login with auto-MFA** via a TOTP secret in `secrets.env` (the `pyotp` library generates the six-digit code at browser launch time).

Amazon is unusual in that every Camoufox launch does a fresh login in about 60 seconds rather than trying to persist a session. I tried persistent sessions. They fail. Camoufox's cookie jar doesn't survive restarts reliably, and even when it does, Amazon marks the session stale after a surprisingly short window. The move that actually works is "log in from scratch every time, and automate the MFA so it is free."

The TOTP secret lives in `secrets.env`, which is a separate file from the regular `.env`, is gitignored, is `chmod 600`, and is listed as a second `env_file` in the host wrapper's source chain. This is load-bearing: a leaked `.env` that gets dumped into a paste buffer is bad; a leaked `secrets.env` with your Amazon TOTP secret is an account takeover. See [Ch 08 — Security and hardening](08-security-and-hardening.md) for the full TOTP-secret-handling story.

The auto-MFA decision is load-bearing for a different reason. Every Camoufox launch is "a new device" from Amazon's perspective, which means every launch triggers MFA. If you don't automate the MFA, every overnight cron fails on the MFA prompt and you wake up to an alert. If you automate it, every launch is self-healing and you stop thinking about auth. I documented the tradeoff explicitly in a memory file titled *"browser auth is expensive"*: never ask the operator to manually re-auth; build auto-recovery or don't build the feature at all. Amazon is where that rule was forged.

## What Hilda cannot do yet

Being honest about the outstanding issues:

- **Amazon new-item search is not implemented.** Reorder (past purchases) works. "Find me a new humidifier" does not. This was deferred as post-1.0 work and I haven't come back to it.
- **Costco order pagination is hardcoded to 50.** If you have more than 50 active Costco orders, the older ones silently drop off. I have fewer than ten, so I haven't felt this, but if you are a heavy Costco user this is a real limitation.
- **The Costco credential-reauth fallback is fragile.** Act III fixed the setCookie bug, but the post-KMSI flow (the "Stay signed in?" interstitial) still wedges sometimes. In practice the headless Step 0 path wins every time and the fallback is never exercised, but if the refresh_token ever expires in a way that Step 0 can't recover from, the fallback will need more work.
- **The Amazon HTML parser is fragile to DOM changes.** It uses a custom `div.order-card` parser that will break the moment Amazon changes its order-history layout. This is a "when, not if" — the mitigation is a loud failure mode plus a post-mortem.

## Deployment walkthrough

The seven-step arc from [Ch 07-0](07-0-your-first-agent.md) is the backbone. What follows is the Hilda-specific stuff that goes on top. Read this section end-to-end before you start, and do not try to follow it in parallel with another deploy — Hilda needs your full attention.

### Pre-step 0 — The residential proxy

Before anything else: sign up for a sticky residential proxy. I use [DataImpulse](https://dataimpulse.com/) with a US pool and the port-10000 sticky-session mode. Alternatives exist (IPRoyal, Bright Data, Smartproxy); I can't vouch for any specific one against Costco specifically, because I haven't tested them. What matters:

- **Sticky sessions, not rotating.** Costco's auth subdomain enforces per-session IP consistency. A rotating pool will work for the catalog pages and fail at the auth.
- **US residential IPs.** ISPs in the datacenter-IP blocklist (Hetzner, DigitalOcean, AWS, anything with AS numbers that show up on lookup tools as "hosting") will be 403'd within minutes.
- **Sticky duration ~30 minutes minimum.** Shorter sticky windows break the Camoufox flows mid-session.

Stash the proxy URL in `.env` as `PROXY_URL=http://user:pass@host:10000`. All of the Camoufox-based scripts read this env var.

### Pre-step 1 — Accounts with TOTP enabled

On both Amazon and Costco, turn on 2FA with an authenticator app (not SMS). **Save the TOTP seed** — the QR-code setup screen usually has a "can't scan?" link that reveals the base32 seed string. Write it down. You will be putting it in `secrets.env` on the VPS.

You will also want the Amazon and Costco passwords in `.env`:

```bash
AMAZON_USERNAME="sam.smith@example.com"
AMAZON_PASSWORD="..."
COSTCO_EMAIL="sam.smith@example.com"
COSTCO_PASSWORD="..."
PROXY_URL="http://user:pass@host:10000"
```

And in `secrets.env` (chmod 600, separate from `.env`):

```bash
AMAZON_TOTP_SECRET="JBSWY3DPEHPK3PXP"
```

The split is deliberate: `.env` has operational credentials that live in the normal config surface; `secrets.env` has the thing that would, by itself, let an attacker pass Amazon's MFA gate on a new device. Keep them apart.

### Pre-step 2 — Initial Costco token capture (one-time)

The Costco daemon needs an initial `refresh_token` to bootstrap its headless-first path. You have two options:

1. **PKCE probe on the VPS** (`costco-pkce-probe.py`): launches Camoufox silently with saved SSO cookies, runs the PKCE authorize flow against the public client id, captures the `refresh_token`. Needs you to have already done one manual login to seed the SSO cookie. Fastest.
2. **Local Chrome capture** (`costco-capture-session.py`): run on your dev box with a real Chrome, log in manually, the script extracts the SSO cookie and refresh_token from the browser. SCP the result to the VPS. Slower but reliable for first-time setup.

Either way, the output is a `cache/costco-tokens.json` file containing a valid `refresh_token`. Once that file exists, the daemon's Step 0 path takes over and the browser steps become a rarely-used safety net. Keep this file backed up — if you lose it, you're back to running one of these bootstrap flows.

### Pre-step 3 — System dependencies

Hilda needs more host packages than the rest of the fleet. Camoufox has its own browser download (~150 MB), `pillow` needs `libjpeg` / `libfreetype` / `zlib` / `libpng` headers to build, and the Camoufox headful fallback needs `xvfb` + `openbox`. Run both install scripts in order:

```bash
ssh openclaw@<vps> "sudo ~/repo/ops/scripts/install-host-system-deps.sh"
ssh openclaw@<vps> "~/repo/ops/scripts/install-host-deps.sh"
```

The first is sudo and installs apt packages. The second installs the Python packages via `pip --user` and fetches the Camoufox browser. Both are idempotent.

### Steps 1-7 of the arc

From here you're on the standard seven-step arc from [Ch 07-0](07-0-your-first-agent.md). The deltas for Hilda:

- **Step 1 (Telegram bot)**: create the Hilda bot via @BotFather, save the token as `SHOPPING_BOT_TOKEN` in `~/clawford/.env`.
- **Step 2 (bootstrap configs)**: `python3 agents/shared/deploy.py shopping --bootstrap-configs`. Edit USER.md with your name and preferences, IDENTITY.md with whatever voice you want Hilda to speak in, SOUL.md with the purchasing boundaries you want her to respect.
- **Step 3 (scripts)**: all shopping scripts are already committed to git — there's no net-new authoring for a basic deploy. If you want to add a new retailer, that's a separate project that deserves its own planning pass.
- **Step 4 (manifest.json)**: the example ships with all the Hilda crons pre-declared. Review `agents/shopping/manifest.json.example`, copy to `manifest.json`, adjust the Telegram bot binding to your new bot token env var if you named it differently.
- **Step 5 (host-cron registration)**: the shopping crons are already in `ops/scripts/install-host-cron.sh`. No edit needed unless you want to change schedules.
- **Step 6 (deploy)**: SSH, pull, run `python3 agents/shared/deploy.py shopping --yes-updates`. The deploy tool will copy all the scripts and configs into `~/.clawford/shopping-workspace/`, handle the `chattr +i` on SOUL and IDENTITY, and capture a backup tarball.
- **Step 7 (install-host-cron.sh)**: run it on the VPS. Seven shopping-related cron entries should land in `crontab -l`:
  - `*/12 * * * *` — Costco token refresh (the daemon step 0 loop, though the daemon is actually long-running; this is the safety-net cron)
  - `30 10 * * *` — delivery digest compose (populates the 5 AM PT fleet brief)
  - `*/30 * * * *` — heartbeat
  - plus whichever S&S management crons you have enabled
- **Step 8 (smoke test)**: manually fire the Costco daemon's Step 0 via `python3 ~/.clawford/shopping-workspace/scripts/costco-token-daemon.py --once` and verify it prints a fresh id_token to stdout. Then fire the delivery-digest manually and verify it produces a `cache/morning-brief-ready.txt` file with your actual orders in it. If both work, wait one morning cycle and the 5 AM PT fleet delivery will include Hilda's section.

## Pitfalls you'll hit

> 🧨 **Pitfall.** Trying to deploy Hilda on a bare VPS IP without a residential proxy because you "want to see if it works first." **Why:** it won't, and the debugging won't point you at the problem — you'll get plausible-looking 403 errors on specific API endpoints that look like they could be auth issues or rate limits. Costco's Akamai fingerprint is the real cause and it won't show up in any error message. **How to avoid:** buy the proxy subscription on day one, stash the URL in `.env` before you even think about running the daemon. `\$1/month` is the cheapest debugging you can buy.

> 🧨 **Pitfall.** Putting the Amazon TOTP secret in `.env` instead of `secrets.env`. **Why:** `.env` gets dumped into paste buffers, screen-shared into Zoom, copied into Slack DMs when you're asking someone for help. A leaked `.env` with regular credentials is bad but recoverable (rotate, investigate). A leaked `.env` with a TOTP secret is an account takeover — TOTP seeds do not get invalidated by a password reset. **How to avoid:** keep `secrets.env` as a separate file, `chmod 600`, sourced as a second `env_file` in the host wrapper source chain. Never cat it. Never grep it. Never include it in a log dump.

> 🧨 **Pitfall.** Using `time.sleep()` in a Playwright route-handler polling loop. **Why:** synchronous Playwright route handlers dispatch on the main thread, and `time.sleep()` blocks the main thread, and a blocked main thread will not dispatch a handler even if the request that should have triggered it already arrived. Your route interceptor goes to sleep at the exact moment it's supposed to be listening. **How to avoid:** use `page.wait_for_timeout()` anywhere you are waiting for a route handler to fire. `time.sleep()` is fine for non-Playwright delays, not fine anywhere Playwright is handling network events.

> 🧨 **Pitfall.** Expecting Costco's credential-reauth fallback to work when Step 0 is broken. **Why:** the fallback path (log in from scratch with password) has two independent bugs that are both in the post-KMSI flow — the "Stay signed in?" interstitial sometimes wedges the form-POST, and the SETTINGS object isn't always populated when the extractor expects it. In practice the Step 0 public-client path has never failed in production, so the fallback has never had to run. If Step 0 ever does fail, expect to debug the fallback by hand. **How to avoid:** back up `cache/costco-tokens.json` regularly so that if the refresh_token expires you can restore from a recent copy and re-bootstrap via `costco-pkce-probe.py` rather than relying on Step 3.

> 🧨 **Pitfall.** Running Camoufox and expecting fast cold-starts. **Why:** every Camoufox launch is 30-45 seconds of profile load, anti-detect initialization, and page-level warmup. If your cron schedule doesn't account for this, you'll have overlapping launches stepping on each other's proxy sessions. **How to avoid:** for anything that needs frequent refresh (the Costco daemon is the canonical case), use a long-running supervisor that keeps one browser warm. For the Amazon S&S management flows, where freshness is fine and the browser only runs at the 5 AM PT fleet window, accept the startup overhead because it only matters once per day.

> 🧨 **Pitfall.** Assuming the `amazonorders` PyPI library will keep working forever. **Why:** it's parsing Amazon's order-history HTML. Amazon changes its order-history HTML. The library has an active maintainer and has been reasonably quick to adapt in my experience, but this is not an API contract, it's an implementation dependency on one of the most actively-defended surfaces on the internet. **How to avoid:** set up a heartbeat probe that confirms `amazon-orders.py` returns at least one order per day (or whatever matches your usage), and when the probe goes red, the first thing to check is whether the library has a new release.

## See also

- [Ch 02 — What Clawford Isn't](02-what-clawford-isnt.md) — the broader story of why the runtime looks like this.
- [Ch 06 — Infra setup](06-infra-setup.md) — the shared library tiers (camoufox_proxy, retry_policy) that Hilda is the canonical consumer for.
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the manifest shape, the script contract, the canonical SSH+pull+deploy path.
- [Ch 07-0 — Your first agent](07-0-your-first-agent.md) — the seven-step arc this chapter leans on.
- [Ch 07-7 — Auth architectures](07-7-auth-architectures.md) — the cross-cutting reference for OAuth-then-SCP, Camoufox + MFA + residential proxy, and Chrome DevTools patterns. Half of Hilda's stack is documented there.
- [Ch 08 — Security and hardening](08-security-and-hardening.md) — the full TOTP-secret-handling story and the `secrets.env` convention.
- [`agents/shopping/manifest.json.example`](../agents/shopping/manifest.json.example) — the canonical manifest with every Hilda cron pre-declared.
- [`agents/shared/camoufox_proxy.py`](../agents/shared/camoufox_proxy.py) and [`agents/shared/retry_policy.py`](../agents/shared/retry_policy.py) — the Tier 3 library modules Hilda consumes.
