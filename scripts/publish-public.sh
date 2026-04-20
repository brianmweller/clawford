#!/usr/bin/env bash
# publish-public.sh — build the public clawford repo as a scrubbed derived
# export of the private canonical repo.
#
# Usage:
#   scripts/publish-public.sh [--dry-run] [--push]
#
#   --dry-run  (default): clone, scrub, verify, leave the scrubbed tree
#              in /tmp/clawford-publish-<timestamp>/ for inspection.
#              No remote operations.
#   --push:    after scrub + verify, push the scrubbed tree to
#              git@github.com:samsmith/clawford.git (force).
#
# Prerequisites:
#   - git filter-repo installed (`pip install git-filter-repo`)
#   - ops/publish/scrub-paths.txt  (path deletions for filter-repo)
#   - ops/publish/replacements.txt (text substitutions for filter-repo)
#   - The private repo is clean (no uncommitted changes).
#
# What this script guarantees:
#   - The scrub runs on a throwaway clone, NOT the working tree.
#   - If verify fails, the script exits non-zero before any push.
#   - --push is never implicit; the operator must pass it explicitly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRUB_PATHS="$REPO_ROOT/ops/publish/scrub-paths.txt"
REPLACEMENTS="$REPO_ROOT/ops/publish/replacements.txt"
PUBLIC_REMOTE="git@github.com:samsmith/clawford.git"

MODE="dry-run"
for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE="dry-run" ;;
    --push)    MODE="push" ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

ts="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="/tmp/clawford-publish-$ts"

echo "== publish-public.sh =="
echo "Mode:        $MODE"
echo "Source:      $REPO_ROOT"
echo "Work dir:    $WORK"
echo "Scrub paths: $SCRUB_PATHS"
echo "Replacements:$REPLACEMENTS"
echo

# Preflight
[[ -f "$SCRUB_PATHS" ]] || { echo "missing $SCRUB_PATHS" >&2; exit 3; }
[[ -f "$REPLACEMENTS" ]] || { echo "missing $REPLACEMENTS" >&2; exit 3; }
command -v git-filter-repo >/dev/null 2>&1 || \
  git filter-repo --help >/dev/null 2>&1 || {
    echo "git-filter-repo not installed. pip install git-filter-repo" >&2
    exit 4
  }

# Refuse to scrub a dirty working tree — the throwaway clone inherits
# committed state only, so any uncommitted PII would be silently missed.
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  echo "Private repo has uncommitted changes. Commit or stash first." >&2
  git -C "$REPO_ROOT" status --short >&2
  exit 5
fi

# Clone (local, no hardlinks — we want an independent object store so
# filter-repo's history rewrite doesn't touch the source repo).
echo "[1/5] Cloning..."
git clone --no-local "$REPO_ROOT" "$WORK"
cd "$WORK"

# Scrub 1: remove the ToS-violating paths + archived guides + planning docs.
echo "[2/5] Scrubbing paths..."
git filter-repo --force --invert-paths --paths-from-file "$SCRUB_PATHS"

# Scrub 2: text substitutions for any PII that survives the path scrub
# (test-file literals, docstring references).
echo "[3/5] Applying text replacements..."
git filter-repo --force --replace-text "$REPLACEMENTS"

# Verify: any scrubbed path that survived is a failure.
echo "[4/5] Verifying scrub..."
fail=0
while IFS= read -r path; do
  # Strip comment and whitespace.
  path="${path%%#*}"
  path="$(echo "$path" | xargs)"
  [[ -z "$path" ]] && continue
  # Strip trailing slash for file existence check.
  if [[ -e "$WORK/$path" || -e "$WORK/${path%/}" ]]; then
    echo "  FAIL: $path survived the scrub" >&2
    fail=1
  fi
done < "$SCRUB_PATHS"

# Verify: PII literals from replacements.txt must not appear at HEAD.
while IFS= read -r line; do
  line="${line%%#*}"
  line="$(echo "$line" | xargs)"
  [[ -z "$line" ]] && continue
  literal="${line%%==>*}"
  [[ -z "$literal" ]] && continue
  if git -C "$WORK" grep -q -F -- "$literal" 2>/dev/null; then
    echo "  FAIL: PII literal still present at HEAD: $literal" >&2
    fail=1
  fi
done < "$REPLACEMENTS"

if [[ "$fail" -ne 0 ]]; then
  echo "Scrub verification FAILED. Inspect $WORK and fix inputs." >&2
  exit 6
fi
echo "  ok"

# Push or report.
if [[ "$MODE" == "push" ]]; then
  echo "[5/5] Pushing scrubbed history to $PUBLIC_REMOTE (force)..."
  git remote add public "$PUBLIC_REMOTE"
  git push public master --force
  echo "Done. Verify the public repo on GitHub before flipping visibility."
else
  echo "[5/5] Dry-run complete."
  echo "Scrubbed tree is at: $WORK"
  echo "Inspect it manually, then re-run with --push to publish."
fi
