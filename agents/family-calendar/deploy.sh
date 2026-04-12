#!/usr/bin/env bash
# Mistress Mouse deploy — delegates to the unified Python deploy tool.
# Legacy shell version lives in git history (pre-2026-04-12).
# Manifest: agents/family-calendar/manifest.json
exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
