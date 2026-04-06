#!/usr/bin/env bash
# Git push wrapper for Mr Fixit
# Usage: bash ~/repo/scripts/git-push.sh
#
# Runs pre-push safety check, then pushes if clean.
# Mr Fixit calls this as a single command to avoid
# exec approval issues with compound shell commands.

set -euo pipefail

cd "$(dirname "$0")/.."
git fetch origin

COUNT=$(git rev-list --count origin/master..HEAD 2>/dev/null || echo 0)

if [ "$COUNT" -eq 0 ]; then
    echo 'NO_UNPUSHED_COMMITS'
    exit 0
fi

echo "$COUNT unpushed commit(s). Running pre-push check..."
bash scripts/pre-push-check.sh
CHECK_RESULT=$?

if [ $CHECK_RESULT -eq 0 ]; then
    git push origin master
    echo "PUSHED"
else
    echo "PRE-PUSH CHECK FAILED — not pushing. Review the warnings above."
    exit 1
fi
