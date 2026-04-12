#!/usr/bin/env bash
# fix-it deploy — delegates to the unified Python deploy tool.
# Manifest: agents/fix-it/manifest.json
OPENCLAW_AGENT_ID="fix-it" exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
