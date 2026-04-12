#!/usr/bin/env bash
# Huckle Cat deploy — delegates to the unified Python deploy tool.
# Legacy shell version lives in git history (pre-2026-04-12).
# Manifest: agents/connector/manifest.json
#
# NOTE: Huckle Cat is NOT yet onboarded on the VPS. First run requires:
#   1. Create bot via @BotFather, save token as CONNECTOR_BOT_TOKEN in
#      ~/openclaw/.env on the VPS.
#   2. Run `openclaw agents add connector` interactively inside the
#      gateway container (this step needs a TTY — deploy.py cannot yet
#      drive the onboarding wizard).
#   3. `/start` the Huckle Cat bot on Telegram and approve pairing via
#      `openclaw pairing approve telegram <CODE>`.
#   4. Then run this script (which delegates to deploy.py) to install
#      workspace files, register crons, and wire up exec approvals.
exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
