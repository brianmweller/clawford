# Chapter 3: Dropbox Sync

Dropbox on a headless VPS is the hardest easy thing in this setup. Budget an hour.

---

## Why Dropbox

The shared brain lives on the VPS filesystem. If the VPS dies, the brain dies with it. Dropbox provides:

- **Automatic offsite backup** — if the VPS is destroyed, the brain survives
- **Any-device access** — browse your agents' data from your phone or laptop via Dropbox
- **Conflict detection** — when two agents write simultaneously, Dropbox creates "conflicted copy" files that Fix-It monitors

It's not the best sync tool. It's the one that works with the least infrastructure.

## Install the Dropbox daemon

```bash
cd ~ && curl -Ls 'https://www.dropbox.com/download?plat=lnx.x86_64' | tar xzf -
```

## Link to your account

Start the daemon in the foreground — it will print an authorization URL:

```bash
~/.dropbox-dist/dropboxd
```

Copy the URL it prints, open it in your browser, and log in with your Dropbox account. After authorizing, you'll see "This computer is now linked to Dropbox." Press Ctrl+C.

## Start the daemon in the background

```bash
nohup ~/.dropbox-dist/dropboxd > /dev/null 2>&1 &
```

Install the CLI for status checks:

```bash
sudo apt install -y nautilus-dropbox
dropbox status
```

## Selective sync — CRITICAL

If your Dropbox has more than just the brain folder, you **must** exclude everything else immediately. Without this, the VPS downloads your entire Dropbox and fills the disk.

> **WARNING:** Apply exclusions IMMEDIATELY after linking. If you wait, the daemon starts downloading hundreds of thousands of files.

```bash
# Create directories first (exclude command needs targets)
mkdir -p ~/Dropbox/Archive ~/Dropbox/Personal ~/Dropbox/Projects

# Exclude with FULL PATHS (relative paths silently fail)
dropbox exclude add ~/Dropbox/Archive ~/Dropbox/Personal ~/Dropbox/Projects

# Verify
dropbox exclude list
```

> **WARNING:** Exclusions require **full paths**. `dropbox exclude add Archive` silently does nothing. Always use `~/Dropbox/Archive`.

Only `openclaw-backup/` should remain syncing.

## Known issues

### Ghost folders

Dropbox says "Up to date" but files aren't syncing. `dropbox filestatus ~/Dropbox/openclaw-backup/README.md` shows "unwatched."

**Cause:** The daemon's internal state doesn't match the filesystem. This happens after re-linking or when the folder existed in the cloud before the daemon was set up.

**Fix:** Delete the empty cloud folder from dropbox.com, then recreate the files on the VPS. The daemon will upload them as new content.

### Daemon stops silently

After SSH disconnect, the daemon may stop. It does not produce an error.

**Fix:** Run under systemd or check `dropbox status` periodically. Fix-It's heartbeat cron will detect if the brain files stop updating.

### "Dropbox isn't responding"

The daemon is overloaded (usually during initial sync with too many files).

**Fix:** Wait 30 seconds and retry. If persistent, `pkill -f dropbox && sleep 3 && nohup ~/.dropbox-dist/dropboxd > /dev/null 2>&1 &`.

### After re-linking

If you unlink and re-link the daemon (e.g., after a VPS rebuild), you must re-apply all exclusions immediately. The re-linked daemon starts with no exclusions and will attempt to download your entire Dropbox.

```bash
# Re-apply exclusions immediately after re-linking
dropbox exclude add ~/Dropbox/Archive ~/Dropbox/Personal ~/Dropbox/Projects ~/Dropbox/Startup
# ... add all folders except openclaw-backup
```

## Verification

Create a test file and confirm it appears in the cloud:

```bash
echo "sync test $(date)" > ~/Dropbox/openclaw-backup/sync-test.txt
sleep 30
dropbox filestatus ~/Dropbox/openclaw-backup/sync-test.txt
```

Should show "up to date." Also check dropbox.com to confirm the file appears. Then clean up:

```bash
rm ~/Dropbox/openclaw-backup/sync-test.txt
```

## Conflict files

When two processes write to the same file simultaneously, Dropbox creates a file like:

```
active (conflicted copy 2026-04-02).md
```

Fix-It monitors for these every 2 hours and alerts you on Telegram. The append-only convention minimizes this risk, but `commitments/active.md` is the most likely candidate since it allows in-place edits.

> **Do NOT auto-merge conflict files.** Let Fix-It alert you, then resolve manually.

---

Next: [Chapter 4 — First Agent](04-first-agent.md)
