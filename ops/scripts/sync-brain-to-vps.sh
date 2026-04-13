#!/usr/bin/env bash
# sync-brain-to-vps.sh — Push ops/brain/* → VPS ~/Dropbox/openclaw-backup/.
#
# The Clawford shared brain has two layers:
#
#   1. State (agent-owned, Dropbox-only): status.md, people/, facts/,
#      commitments/, tasks/, obsidian/briefings/, archive/. These are
#      updated by live agents at runtime and MUST NOT be overwritten
#      from git — the VPS is the source of truth.
#
#   2. Declarative config/code (git-tracked at ops/brain/*): scripts
#      that crons execute, docs, fix-it's KNOWN_ISSUES/probation,
#      agent rules templates. These live in git and flow one way:
#      git → VPS. That's what this script pushes.
#
# Runs in dry-run by default — prints what would change. Use --apply
# to execute. Refuses to clobber files that have been modified on the
# VPS side more recently than the git version unless --force is also
# passed (the safety net for "I edited this on the VPS by mistake
# and forgot to pull it back to git" situations).
#
# Usage:
#   bash ops/scripts/sync-brain-to-vps.sh              # dry-run
#   bash ops/scripts/sync-brain-to-vps.sh --apply      # execute
#   bash ops/scripts/sync-brain-to-vps.sh --apply --force  # override VPS-newer check
#
# Prereqs: ssh access to openclaw@198.51.100.42 configured.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BRAIN_SRC="$REPO_ROOT/ops/brain"
VPS_HOST="openclaw@198.51.100.42"
VPS_DEST="Dropbox/openclaw-backup"

APPLY=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --force) FORCE=1 ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

if [ ! -d "$BRAIN_SRC" ]; then
    echo "ERR: $BRAIN_SRC does not exist" >&2
    exit 1
fi

# Enumerate git-tracked files under ops/brain/. Exclude __pycache__.
mapfile -t FILES < <(
    cd "$REPO_ROOT" && \
    git ls-files 'ops/brain/*' | grep -v '__pycache__'
)

if [ "${#FILES[@]}" -eq 0 ]; then
    echo "ERR: no git-tracked files under ops/brain/" >&2
    exit 1
fi

echo "=== sync-brain-to-vps.sh ==="
echo "Source:   $BRAIN_SRC"
echo "Target:   $VPS_HOST:$VPS_DEST/"
echo "Files:    ${#FILES[@]}"
echo "Mode:     $([ $APPLY -eq 1 ] && echo APPLY || echo DRY-RUN)"
echo

# Path remap: ops/brain/<rest> → <VPS_DEST>/<rest>
remap() {
    echo "${1#ops/brain/}"
}

newer_on_vps() {
    # 0 if the VPS copy is strictly newer than git HEAD for this file.
    # Uses stat on VPS vs. git log commit-time for the source file.
    local rel_src="$1"
    local rel_dst="$2"
    local git_ts
    git_ts=$(cd "$REPO_ROOT" && git log -1 --format=%ct -- "$rel_src" 2>/dev/null || echo 0)
    local vps_ts
    vps_ts=$(ssh "$VPS_HOST" "stat -c %Y '$VPS_DEST/$rel_dst' 2>/dev/null || echo 0")
    [ "$vps_ts" -gt "$git_ts" ]
}

CHANGED=0
SKIPPED_VPS_NEWER=0
IDENTICAL=0

for rel in "${FILES[@]}"; do
    rel_dst="$(remap "$rel")"
    local_hash=$(cd "$REPO_ROOT" && git hash-object "$rel")
    vps_hash=$(ssh "$VPS_HOST" "test -f '$VPS_DEST/$rel_dst' && git hash-object '$VPS_DEST/$rel_dst' 2>/dev/null || echo missing")

    if [ "$vps_hash" = "$local_hash" ]; then
        IDENTICAL=$((IDENTICAL + 1))
        continue
    fi

    status="UPDATE"
    if [ "$vps_hash" = "missing" ]; then
        status="NEW"
    elif [ "$FORCE" -eq 0 ] && newer_on_vps "$rel" "$rel_dst"; then
        status="SKIP (VPS newer — pass --force to override)"
        SKIPPED_VPS_NEWER=$((SKIPPED_VPS_NEWER + 1))
        printf '  %-40s %s\n' "$rel_dst" "$status"
        continue
    fi

    CHANGED=$((CHANGED + 1))
    printf '  %-40s %s\n' "$rel_dst" "$status"

    if [ "$APPLY" -eq 1 ]; then
        ssh "$VPS_HOST" "mkdir -p '$VPS_DEST/$(dirname "$rel_dst")'"
        scp -q "$REPO_ROOT/$rel" "$VPS_HOST:$VPS_DEST/$rel_dst"
    fi
done

echo
echo "Identical:         $IDENTICAL"
echo "To update:         $CHANGED"
echo "Skipped VPS-newer: $SKIPPED_VPS_NEWER"
if [ "$APPLY" -eq 0 ] && [ "$CHANGED" -gt 0 ]; then
    echo
    echo "Dry-run only. Re-run with --apply to execute."
fi
if [ "$SKIPPED_VPS_NEWER" -gt 0 ]; then
    echo
    echo "WARN: $SKIPPED_VPS_NEWER file(s) on the VPS are newer than the git"
    echo "      version. Investigate each one — likely uncommitted VPS-side"
    echo "      edits that should be pulled back into git first. Use --force"
    echo "      to overwrite anyway."
fi
