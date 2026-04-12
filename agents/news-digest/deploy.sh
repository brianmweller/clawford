#!/usr/bin/env bash
# news-digest deploy — delegates to the unified Python deploy tool.
# Manifest: agents/news-digest/manifest.json
OPENCLAW_AGENT_ID="news-digest" exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
