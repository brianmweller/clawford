#!/bin/bash
# install-git-hooks.sh — symlink tracked hooks from ops/git-hooks into
# .git/hooks so `git pull` triggers the auto-deploy post-merge hook.
#
# Idempotent: re-run safely. Removes any prior hook of the same name
# so symlink swaps cleanly.
#
# Run from repo root:
#   bash ops/scripts/install-git-hooks.sh

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_SRC="$REPO_ROOT/ops/git-hooks"
HOOKS_DST="$REPO_ROOT/.git/hooks"

if [ ! -d "$HOOKS_SRC" ]; then
  echo "ERROR: $HOOKS_SRC not found" >&2
  exit 1
fi
if [ ! -d "$HOOKS_DST" ]; then
  echo "ERROR: $HOOKS_DST not found (not a git checkout?)" >&2
  exit 1
fi

for src in "$HOOKS_SRC"/*; do
  name="$(basename "$src")"
  dst="$HOOKS_DST/$name"
  chmod +x "$src"
  rm -f "$dst"
  ln -s "../../ops/git-hooks/$name" "$dst"
  echo "installed: $dst -> ops/git-hooks/$name"
done
