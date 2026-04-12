#!/usr/bin/env bash
# family-calendar deploy — delegates to the unified Python deploy tool.
# Manifest: agents/family-calendar/manifest.json
OPENCLAW_AGENT_ID="family-calendar" exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
