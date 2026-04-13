#!/usr/bin/env bash
set -euo pipefail

MANIFEST="/opt/config/skills-manifest.txt"
WORKDIR="/home/node/.openclaw/workspace"

###############################################################################
# Seed workspace templates (no-clobber -- will not overwrite existing files)
###############################################################################
TEMPLATES="/opt/workspace-templates"
if [[ -d "$TEMPLATES" ]]; then
  echo "[entrypoint] Seeding workspace templates ..."
  cp -rn "$TEMPLATES"/. "$WORKDIR"/
fi

###############################################################################
# Install ClawHub skills from the manifest (if present)
###############################################################################
if [[ -f "$MANIFEST" ]]; then
  echo "[entrypoint] Installing ClawHub skills from manifest ..."
  while IFS= read -r line; do
    # Skip blank lines and comments
    line="${line%%#*}"
    line="$(echo "$line" | xargs)"
    [[ -z "$line" ]] && continue

    # Skip if already installed
    if [[ -d "$WORKDIR/skills/$line" ]]; then
      echo "[entrypoint]   $line (already installed)"
      continue
    fi

    echo "[entrypoint]   installing $line"
    clawhub install "$line" --workdir "$WORKDIR" || {
      echo "[entrypoint] WARNING: Failed to install $line -- continuing"
    }
  done < "$MANIFEST"
  echo "[entrypoint] Skill installation complete."
else
  echo "[entrypoint] No skills manifest found -- skipping skill install."
fi

###############################################################################
# Start agent background services (bind-mounted, survives rebuilds)
###############################################################################
STARTUP="/home/node/.openclaw/shopping-workspace/scripts/on-startup.sh"
if [[ -x "$STARTUP" ]]; then
  echo "[entrypoint] Running shopping startup hook ..."
  "$STARTUP" &
fi

###############################################################################
# Belt-and-suspenders Telegram bot-command restore
#
# Durable path: openclaw.json's per-account customCommands + commands.native:false
# is the authoritative source (DEPLOY.md step 10a). openclaw's channel-sync
# pushes it during gateway startup. This hook is step 10b: a direct Bot API
# setMyCommands call that runs ~25s AFTER the gateway starts, so any stale
# openclaw sync clobber gets overwritten by our canonical commands. No-op
# if openclaw.json already pushed the correct set.
#
# The script is bind-mounted via /home/node/repo so updates flow through
# git without rebuilding the image.
###############################################################################
BOT_CMDS="/home/node/repo/ops/scripts/set-bot-commands.sh"
if [[ -x "$BOT_CMDS" ]]; then
  echo "[entrypoint] Scheduling bot-command restore in 25s ..."
  (sleep 25 && bash "$BOT_CMDS" 2>&1 | sed 's/^/[bot-cmds] /') &
fi

###############################################################################
# Hand off to the real command (CMD from docker-compose)
###############################################################################
exec "$@"
