#!/usr/bin/env bash
# Thin wrapper — each agent's deploy.sh becomes:
#   exec bash "$(dirname "$0")/../shared/deploy_wrapper.sh" "$@"
# That calls python3 deploy.py <agent_id> with the agent inferred from the
# wrapper's calling directory. Single source of truth for deploy behavior.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_PY="$SCRIPT_DIR/deploy.py"

# When sourced from agents/<id>/deploy.sh, $0 points at that path — extract id.
CALLER="${BASH_SOURCE[1]:-$0}"
AGENT_ID="$(basename "$(dirname "$(realpath "$CALLER")")")"

if [ -z "$AGENT_ID" ] || [ "$AGENT_ID" = "shared" ]; then
    echo "deploy_wrapper.sh must be invoked from an agent's deploy.sh" >&2
    exit 2
fi

if [ -f ~/openclaw/.env ]; then
    set -a
    source ~/openclaw/.env
    set +a
fi

exec python3 "$DEPLOY_PY" "$AGENT_ID" "$@"
