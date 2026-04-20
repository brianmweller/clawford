# VPS setup

*Last updated: 2026-04-17 · Reading time: ~15 min · Difficulty: moderate*

**TL;DR**

- Use Terraform to provision the VPS, not the Hetzner console. The whole box should be reproducible from a git repo because rebuilding it is a routine operation, not a disaster.
- Hetzner cpx31 in Hillsboro, Oregon works well. Other providers work too, but the community Terraform module this guide wraps targets Hetzner specifically.
- Lock down SSH (key-only, non-root, restricted CIDRs), put Tailscale on top. Nothing in a Clawford fleet needs to be reachable from the public internet — not SSH, not an API endpoint, nothing.
- **Residential egress is a first-class dependency** for any Tier 3 integration against a vendor with active bot defense. Buy a sticky residential proxy (not a rotating pool) — or, better, run a SOCKS5 tunnel to your home network over Tailscale. Test whichever you pick before deploying a Tier 3 agent.
- `codex` is the one binary that has to work on the VPS. Authenticate it once on a laptop, SCP the credential file across, and verify with a one-shot `codex infer "ping"`.
- Never develop on the VPS. Code lives in local git; the shared brain lives in Dropbox. The VPS just mounts both.

## What Ch 04 gets you

This chapter starts from a Hetzner account and an SSH key. It ends with:

- A provisioned, SSH-hardened, firewalled VPS reachable via Tailscale.
- `codex` installed and authenticated against a ChatGPT Plus subscription.
- A residential proxy wired in and tested end-to-end.
- A first-boot smoke test that can be re-run any time something looks off.

[Ch 05](05-dev-setup.md) (dev setup) and [Ch 06](06-infra-setup.md) (infra setup) pick up from there — Ch 06 is where the shared library, shared brain, host-cron runtime, and `deploy.py` safeguards live.

> **Pre-liberation note.** The current Terraform module still provisions Docker on the VPS because it was written for the OpenClaw-gateway era. A Clawford-native install doesn't need Docker at all — host crons run directly on the host, `codex` is a single binary. The Docker install step is vestigial until the Terraform module catches up. Leave it in place; it's harmless.

## Choosing a provider

Hetzner cpx31 out of Hillsboro, Oregon is the default recommendation. Four vCPUs, 8 GB RAM, 160 GB SSD, ~$30/month. Comfortable for a six-agent fleet with headroom. Hetzner's combination of cheap SSD, a usable console, and a US-West location that doesn't add transatlantic latency to every Telegram round-trip is what makes it the baseline.

Other providers work — DigitalOcean, Linode, Vultr, OVH, AWS Lightsail. What porting away from Hetzner costs is that the community Terraform module this guide leans on is Hetzner-specific. On another provider, most of the work is in the provider-specific `terraform` module; the rest of Clawford doesn't care what's underneath.

A few things to check when provider-shopping:

- **IP reputation.** Some datacenter ranges are more aggressively blocked by retailers and anti-bot services than others. Hetzner's Hillsboro block has been fine for general outbound traffic; for Tier 3 scraping, everything routes through a residential egress anyway (see below).
- **Snapshot / backup pricing.** Hetzner charges 20% of the VPS price for automated snapshots. Cheap. Turn them on.
- **Instance availability by region.** The tier you want may not be in the region you want. Pick one step above what seems necessary — the price delta is small and headroom is cheap.

## What to install on your local machine

Before opening a terraform directory, install locally:

- `terraform` (Hashicorp installer, Homebrew, or winget)
- `hcloud` (the Hetzner CLI)
- An SSH key pair — `ssh-keygen -t ed25519` if one doesn't already exist. This key is the only credential that grants root-level access to the VPS until Tailscale is up; protect it accordingly.
- A Hetzner Cloud API token (console → Security → API Tokens). Generate with read/write scope for the project you'll provision into.

All three of these go into environment variables read by Terraform. None of them should ever hit git.

## The Terraform flow

The shape of the deploy:

1. **Clone the Terraform module.** A community module wraps the Hetzner API plus cloud-init plus firewall resource provisioning. It's what turns a blank Hetzner account into a VPS with Docker, an `openclaw` user, and a firewall in one command.
2. **Create a Hetzner context and upload the SSH key.** `hcloud context create` with the API token, then `hcloud ssh-key create` to register the public key. Save the fingerprint — Terraform needs it.
3. **Fill in `config/inputs.sh`.** API token, SSH key fingerprint, CIDR allowlist, path to the Docker config directory. The example file ships as `inputs.example.sh`; copy it and edit locally.
4. **Fill in `secrets/clawford.env`.** Placeholders for bot tokens and the `PROXY_URL` that get filled in later. Never commit this file.
5. **`terraform plan` and review.** Look for: new server, firewall resource, volume, optional Tailscale resource. Anything else in the plan is a signal to stop and read it. Terraform state is the system's memory of what it thinks it's doing; a surprising plan means that memory is out of date.
6. **`terraform apply`.** Type `yes`. Wait ~2 minutes. Note the output IP.

> ⚠️ **Warning.** The example `inputs.sh` and `secrets/clawford.env` are git-ignored for a reason. Double-check they're in `.gitignore` before filling anything in, and stage by filename rather than `git add -A` on anything under `infra/` or `secrets/`.

At this point the VPS is up, has Docker installed (vestigial — see the pre-liberation note above), has a non-root `openclaw` user, and has an SSH key attached to `root`. The box is not yet locked down, and `codex` is not yet installed. Those are the next two sections.

## SSH hardening and the Tailscale overlay

Key-only SSH is a good start, not the end state. After `terraform apply`, the VPS is reachable from `0.0.0.0/0` on port 22 by default. That's fine for ~15 minutes of setup work; it is not fine as a steady state. Two improvements, cheap and high-leverage:

### Narrow the CIDR, or close SSH entirely

The fastest improvement is to set `TF_VAR_ssh_allowed_cidrs` to a single-IP CIDR (`["203.0.113.42/32"]`, not real), re-run `terraform apply`, and the Hetzner firewall refuses SSH from anywhere else. That's fine as long as the operator always connects from the same place. It falls apart the minute someone travels or their ISP rotates the IP.

The durable answer is Tailscale.

### Install Tailscale

Tailscale is a WireGuard mesh overlay. Install it on the VPS (`curl -fsSL https://tailscale.com/install.sh | sudo sh && sudo tailscale up`), install it on a laptop and phone, and the three of them join the same tailnet with private `100.x.x.x` addresses. The VPS becomes reachable at `clawford-prod` (or whatever the machine is named) from any device logged into Tailscale, and invisible to the public internet at the SSH port.

Concretely, what Tailscale buys:

- SSH over the tailnet from any device, regardless of where the operator is sitting (hotel Wi-Fi, phone hotspot, a café in Lisbon — all fine).
- The ability to close port 22 at the Hetzner firewall entirely (`TF_VAR_ssh_allowed_cidrs='[]'`), so every SSH brute-force bot on the internet sees a closed port. A surprisingly effective upgrade: the noise in `/var/log/auth.log` drops to zero overnight.
- Tailscale Serve for exposing any VPS-side HTTP UI to authorized devices without exposing it to the public internet. No SSH tunnel, no port forward, no TLS cert to manage. Just a URL that only works on the tailnet.

What Tailscale does *not* buy:

- Protection against a compromised device that is already on the tailnet. If someone steals an enrolled laptop, the VPS is reachable. MFA on the Tailscale account is the primary mitigation.
- Protection against stolen or leaked auth keys. Rotate every ~90 days and set them to expire.
- Any protection against vulnerabilities inside the VPS itself. Tailscale gets the operator into the perimeter; the perimeter is still only as strong as what's inside it.

`TF_VAR_enable_tailscale=true` is the default recommendation. Skipping it means maintaining the SSH CIDR allowlist by hand — it works, it's just more friction on every action.

## Firewall basics

The default firewall posture from the Terraform module is conservative and doesn't need tuning:

- **Inbound SSH (22/tcp):** open only to the CIDRs in `ssh_allowed_cidrs`, or closed entirely when Tailscale is up.
- **Inbound Tailscale (UDP 41641):** open when Tailscale is enabled. This is the WireGuard port.
- **Everything else: denied.** No HTTP, no HTTPS, no exposed service ports. A Clawford-native fleet has nothing listening publicly — every scheduled job fires from host crontab and talks outbound to Telegram, Google, and whatever other vendors the agents target via HTTPS; nothing on the VPS needs an inbound port beyond SSH.

> ⚠️ **Warning.** There is never a reason to open an inbound port on the VPS for a Clawford fleet. If a debugging flow seems to require exposing something (a metrics endpoint, a web UI), use Tailscale Serve — not a firewall rule change.

## Installing codex

The one binary the fleet needs on the VPS is `codex`, the OpenAI CLI that rides a ChatGPT Plus subscription for LLM calls. `agents/shared/llm.py` invokes it under the hood, every agent script that calls `infer()` ends up here, and none of the other moving parts exist without it.

The install is two binaries and one credential file.

1. **Install `codex` locally on a laptop** (via Homebrew, `npm install -g @openai/codex`, or whatever the upstream install guide recommends at the time).
2. **Authenticate locally.** `codex login` opens a browser OAuth flow against the ChatGPT Plus account. Approve it. The credential file lands at `~/.codex/auth.json`.
3. **Verify locally.** `codex infer "say hi"` should print a short reply. If it doesn't, stop and fix the local install before touching the VPS.
4. **Install `codex` on the VPS.** Same install pathway; SSH in, run the same install command, verify `codex --version` prints the build string.
5. **SCP the credential file across.** `scp ~/.codex/auth.json openclaw@<tailnet-hostname>:~/.codex/auth.json`. This is the same laptop-then-SCP pattern other auth flows in the fleet use (see [Ch 17 — Auth architectures](17-auth-architectures.md) for the Google OAuth version of the same pattern).
6. **Verify on the VPS.** `ssh openclaw@<tailnet-hostname> "codex infer 'say hi'"`. Expect a short reply.

The credential file is a secret. It grants full access to the ChatGPT Plus account. Treat it like a private key: file mode 0600, never committed, never in a backup that lives somewhere the rest of the fleet doesn't.

> 🧨 **Pitfall.** Running `codex login` on the VPS over SSH. The OAuth flow wants a browser, and the browser-on-a-headless-box path is a fight. **How to avoid:** always run `codex login` on a laptop and SCP the credential file across. This is the same pattern Google OAuth uses for every agent that reads Gmail or Calendar.

## Residential proxies, a first-class dependency

Any Tier 3 integration against a vendor with active bot defense needs residential egress. Retailers and fraud-sensitive portals detect Hetzner IPs — and every other major cloud provider's IP ranges — and serve either outright blocks or a persistent series of CAPTCHAs that a headless browser cannot solve. A datacenter IP does not survive a Tier 3 auth flow. See [Ch 06 — Tier 3 in practice](06-infra-setup.md#tier-3-in-practice) for the patterns.

### Sticky vs rotating

The two shapes of residential proxy:

- **Rotating pool.** Each request (or each short window) comes out of a different residential IP. Great for bulk scraping, *terrible* for anything session-based: the auth cookies were issued to IP A and the next request comes from IP B, so the site decides something just got phished and invalidates the session.
- **Sticky / session-persistent.** One IP stays with the client for minutes to hours. Each connection gets its own sticky session via a session identifier encoded in the credentials the provider expects.

**Sticky is correct for any Tier 3 workflow.** Every login-then-session arc depends on session persistence across scrape → auth → request, and a rotating pool breaks every one of those arcs. This is almost never a pricing decision — most providers (including DataImpulse) treat rotating vs sticky as a config toggle on the same plan, not as separate tiers. It's a correctness choice, not a cost tradeoff.

### DataImpulse

The Clawford fleet uses [DataImpulse](https://dataimpulse.com/)'s Residential Proxy Premium plan against the US residential pool. Other residential-proxy providers should work on the same general pattern, but treat everything below as the *shape* of the setup, not the exact strings — every provider's URL format is different.

DataImpulse's gateway is `gw.dataimpulse.com:823` — a single host and port for both rotating and sticky modes. The session behavior is encoded in the *username*, not in the port. A working URL has the shape:

```
http://<login>__<session-spec>:<password>@gw.dataimpulse.com:823
```

Where `<session-spec>` is a suffix that encodes country code, rotation behavior, and (for sticky) a session identifier. DataImpulse's dashboard has a copy-pastable "Basic URL example" for whichever mode is selected — use that rather than hand-constructing the URL. Get the sticky-suffix format from the DataImpulse docs or the dashboard's config panel; don't guess, and don't trust a pattern remembered from a different provider.

Whichever URL comes out the other side is a secret: it contains the account password in cleartext. Put it in `.env` as `PROXY_URL` and never commit it.

```
PROXY_URL=http://<login>__<session-spec>:<password>@gw.dataimpulse.com:823
```

Scripts that need the proxy read `PROXY_URL` and pass it to Playwright's `browser.launch(proxy={...})`. Scripts that don't need the proxy just ignore it. The seam is at the script level — each script decides whether to route through the proxy based on which tier it belongs to (see [Ch 06 — Infra setup](06-infra-setup.md) for the three-tier model).

### Test it before you need it

Before deploying any agent that depends on the proxy, run a one-off script from the VPS that:

1. Launches a Playwright browser (or even just `curl`) through the proxy.
2. Loads `https://api.ipify.org` or `https://ifconfig.me`.
3. Prints the resulting IP.

Expect a residential IP in the configured country. A Hetzner IP means `PROXY_URL` isn't being read, the environment variable isn't reaching the subprocess, or the credentials are wrong. A residential IP in the *wrong* country means the country code in the session suffix is wrong. Diagnose either before the first real scrape — it's much easier to debug a one-shot test than a mystery 403 in the middle of a cron run.

For sticky verification, make two back-to-back requests against `api.ipify.org` using the same sticky session string. Both should return the same IP. If they differ, the session suffix isn't doing what it looks like, and any shopping flow that depends on it will silently break at checkout.

> 🧨 **Pitfall.** Hitting the provider's rate limits or account lockout because the first real use burns through the traffic quota on failed-auth retries. **Why:** a misconfigured sticky session looks like "auth failed → retry → auth failed → retry" and burns gigabytes in minutes. **How to avoid:** test the proxy in isolation first. Confirm the residential IP. Confirm session stickiness (two requests in the same session should come from the same IP). Only then point an agent at it.

## Disk, backups, snapshots

A practical note on how the VPS's 160 GB of disk gets used:

- **The shared brain.** Tiny today. The whole brain directory lives comfortably under 100 MB — a few months of daily notes, people files, commitments, tasks, and a modest fact set. Expect it to grow as the brain is enriched with more facts, more history, and deeper per-person context over time. cpx31 has headroom even for a much larger brain.
- **The codex binary + model weights cache.** Not large. `codex` itself is tens of megabytes; its runtime cache is small.
- **Deploy-backup tarballs.** `deploy.py` writes a pre-deploy tarball to `~/.clawford/deploy-backups/<agent>-<timestamp>.tar.gz` on every deploy. These pile up unless pruned. Cheap to keep; even cheaper to forget about until debugging a disk-full event at midnight. (The current filesystem path may still show `.openclaw/` pending a final rename; treat the two names as interchangeable.)
- **Playwright browser cache and Camoufox state.** Browser automation eats a surprising amount of disk in cookies, local storage, and cached page assets. Watch `~/.cache/camoufox/` and `~/.cache/ms-playwright/` if unexpected disk growth appears.

**Automatic Hetzner snapshots** — 20% of the VPS monthly price, one snapshot per day, seven-day retention — are the difference between "last night's update broke everything and rolling back is an option" and "last night's update broke everything and the brain has to be rebuilt by hand." Turn them on.

## First-boot smoke test

Before considering the VPS "done" and moving on to [Ch 05](05-dev-setup.md), walk this checklist. Each item is a one-liner that should either pass or give an obvious clue:

- `ssh openclaw@<tailnet-hostname> "echo connected"` → prints `connected`
- `ssh openclaw@<tailnet-hostname> "python3 --version"` → prints a Python 3.11+ version
- `ssh openclaw@<tailnet-hostname> "which codex && codex --version"` → prints the codex binary path and version
- `ssh openclaw@<tailnet-hostname> "codex infer 'say hi'"` → prints a short reply
- `ssh openclaw@<tailnet-hostname> "crontab -l 2>/dev/null | wc -l"` → prints 0 (empty crontab is expected pre-Ch 06)
- `ssh openclaw@<tailnet-hostname> "git clone https://github.com/your-handle/your-repo.git /tmp/test-clone && rm -rf /tmp/test-clone"` → clones and cleans up without a credential prompt
- **Residential-proxy IP check** (from the section above) → prints a residential IP, not the Hetzner IP

If all seven pass, the VPS is ready for [Ch 05](05-dev-setup.md).

## The three invariants to carry into everything else

Three rules that everything else in the guide assumes.

### Never develop on the VPS

This is the first rule that's easy to break and the hardest to unbreak. The code that runs the fleet — scripts, `manifest.json` files, `deploy.py`, configs — lives in local git. The shared brain — facts, people, commitments, tasks, notes — lives in Dropbox and gets bind-mounted at runtime. Neither lives on the VPS as its source of truth. The VPS is a mount point where both are assembled, and nothing else.

What this rule forbids is *on-VPS code edits*: hand-editing a script in the agent workspace, tweaking a cron prompt inside `~/.clawford/<agent>-workspace/CRONS.md`, or patching a deploy tool in place. On-VPS code edits are invisible to git, invisible to `deploy.py`'s drift detection ([Safeguard 4](19-security-and-hardening.md#defense-layer-3-the-deploy-tool-safeguards) catches it), and they're the first thing forgotten when reproducing a working state weeks later. [Safeguard 2](19-security-and-hardening.md#defense-layer-3-the-deploy-tool-safeguards) refuses to deploy if there are uncommitted local changes, and Safeguard 4 refuses to deploy if the VPS workspace has drifted from the last manifest — both exist because this rule was ignored once and the recovery was painful.

Brain writes are the explicit exception. Agents are *expected* to write facts, commitments, daily notes, and similar to the Dropbox-synced brain paths — that's the intended runtime flow, not a violation of the rule. The rule is about code, not about runtime state.

### Bake dependencies into the provisioning step

The Clawford era doesn't use a runtime Docker container, but the same discipline applies to the VPS itself: every binary and package the fleet needs gets installed at provision time by Terraform + cloud-init, not by a runtime script that may or may not run again if a cron recreates its workspace. If a cron needs Playwright, Playwright goes in the provisioning step. If a cron needs a new Python package, the requirements file the provisioning step installs gets updated, not `pip install`'d from inside a running cron.

The older OpenClaw-era guide had this rule as "bake into the `Dockerfile`." The shape is the same; the location moved from a Dockerfile to the provisioning script.

### SCP configs to the host, via git

On-VPS config edits are the same class of forbidden as on-VPS code edits. When a config file needs to change — a real `IDENTITY.md`, a `MEMORY.md`, a `manifest.json` — the change goes through the local git repo and then through `deploy.py`. Never SSH in and edit in place. The deploy tool's drift detection refuses subsequent deploys otherwise, and the "on-VPS hand-edit that was supposed to be temporary" is how live production state diverges from git.

## Pitfalls you'll hit

> 🧨 **Pitfall.** Provisioning with `TF_VAR_ssh_allowed_cidrs='["0.0.0.0/0"]'` and then forgetting to narrow it. **Why:** the default opens SSH to the world, which is fine for the first hour and irresponsible after that. **How to avoid:** either narrow the CIDR to a single IP immediately after `terraform apply` succeeds, or enable Tailscale first and set the CIDR to `'[]'` so SSH is closed to the public internet entirely.

> 🧨 **Pitfall.** SCP-ing `~/.codex/auth.json` into a path the Dropbox daemon also watches. **Why:** Dropbox will happily sync the secret off-VPS, and depending on the destination folder it may replicate to every device on the same account. **How to avoid:** keep `~/.codex/` outside any Dropbox-synced path. The default home-directory install is safe; a custom install under `~/Dropbox/...` is a mistake.

> 🧨 **Pitfall.** Using a rotating residential proxy (or no proxy, or a datacenter proxy) for a Tier 3 workflow. **Why:** a new IP per request invalidates every session the vendor builds, so auth flows that need a consistent egress IP across requests silently fail. Datacenter IPs get blocked outright. **How to avoid:** use a sticky residential egress (paid proxy or home-ISP SOCKS tunnel), test it with a one-off script that prints its egress IP, and confirm session persistence before letting an agent near it.

## See also

- [Ch 02 — What Isn't Clawford?](02-what-isnt-clawford.md) — the decision doc that explains why the runtime looks like this and not like the platform it used to sit on top of.
- [Ch 05 — Dev setup](05-dev-setup.md) — what goes on the laptop to talk to this VPS.
- [Ch 06 — Infra setup](06-infra-setup.md) — the shared library, shared brain, host-cron runtime, and `deploy.py` safeguards that use this VPS as their target.
- [Ch 07 — Intro to agents](07-intro-to-agents.md) — the anatomy of a Clawford agent, built on top of everything this chapter provisioned.
