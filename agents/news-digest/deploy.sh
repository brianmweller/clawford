#!/usr/bin/env bash
# Lowly Worm deploy — delegates to the unified Python deploy tool.
# Legacy shell version lives in git history (pre-2026-04-12).
# Manifest: agents/news-digest/manifest.json
exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
