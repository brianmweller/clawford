#!/usr/bin/env bash
# meetings-coach deploy — delegates to the unified Python deploy tool.
# Manifest: agents/meetings-coach/manifest.json
set -e
cd "$(dirname "$0")/../.."
if python3 -c '' 2>/dev/null; then PY=python3; else PY=python; fi
exec "$PY" agents/shared/deploy.py meetings-coach "$@"
