#!/usr/bin/env bash
# Thin wrapper — each agent's deploy.sh becomes:
#   OPENCLAW_AGENT_ID="$(basename "$(dirname "$(realpath "$0")")")" \
#     exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
# That calls python3 deploy.py <agent_id>, reading the agent id from the
# OPENCLAW_AGENT_ID env var. `exec bash` replaces the shell process, so
# BASH_SOURCE is reset — we CANNOT rely on BASH_SOURCE[1] to find the
# caller. Pre-2026-04-12 code did and silently broke on all invocations.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_PY="$SCRIPT_DIR/deploy.py"

if [ -z "${OPENCLAW_AGENT_ID:-}" ]; then
    echo "deploy_wrapper.sh: OPENCLAW_AGENT_ID not set." >&2
    echo "Each agent's deploy.sh must set it before exec'ing this wrapper." >&2
    exit 2
fi

if [ "$OPENCLAW_AGENT_ID" = "shared" ]; then
    echo "deploy_wrapper.sh: refusing to run with agent id 'shared'." >&2
    exit 2
fi

if [ -f ~/openclaw/.env ]; then
    set -a
    source ~/openclaw/.env
    set +a
fi

exec python3 "$DEPLOY_PY" "$OPENCLAW_AGENT_ID" "$@"
