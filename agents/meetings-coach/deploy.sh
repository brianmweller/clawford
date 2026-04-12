#!/usr/bin/env bash
# meetings-coach deploy — delegates to the unified Python deploy tool.
# Manifest: agents/meetings-coach/manifest.json
OPENCLAW_AGENT_ID="meetings-coach" exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
