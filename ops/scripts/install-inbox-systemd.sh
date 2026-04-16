#!/usr/bin/env bash
set -euo pipefail

REPO=/home/openclaw/repo
UNIT_SRC=$REPO/ops/systemd/clawford-inbox.service
UNIT_DEST=$HOME/.config/systemd/user/clawford-inbox.service
LOG_DIR=$HOME/.clawford/logs

mkdir -p "$(dirname "$UNIT_DEST")"
mkdir -p "$LOG_DIR"

cp "$UNIT_SRC" "$UNIT_DEST"
echo "Copied unit file to $UNIT_DEST"

systemctl --user daemon-reload
systemctl --user enable clawford-inbox.service
echo "Unit reloaded and enabled."

# Graceful cutover: stop any nohup-launched daemon first.
# Two-step kill avoids the SSH self-pkill footgun (gotcha #1).
PID=$(pgrep -f 'python3 agents/shared/telegram_inbox.py' || true)
if [ -n "$PID" ]; then
    echo "Killing nohup daemon (PID $PID)..."
    kill "$PID" || true
    sleep 2
fi

systemctl --user start clawford-inbox.service
sleep 2
systemctl --user status clawford-inbox.service --no-pager
