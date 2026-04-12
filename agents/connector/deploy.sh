#!/usr/bin/env bash
# connector deploy — delegates to the unified Python deploy tool.
# Manifest: agents/connector/manifest.json
OPENCLAW_AGENT_ID="connector" exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
