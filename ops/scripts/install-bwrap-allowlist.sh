#!/usr/bin/env bash
# install-bwrap-allowlist.sh — merge the repo's default bwrap allowlist
# into the on-host opt-in file ~/.clawford/bwrap-allowlist.txt.
#
# Called from deployment workflows. Idempotent: already-present entries
# are left alone; new entries from the default file are appended.
# Comments and blank lines in the default file are skipped. The
# installed file is rewritten atomically (tmp + mv) so a crash mid-
# install can't leave a half-written allowlist.
#
# Per feedback_file_opt_out_pattern.md — reversible persistent opt-
# ins live in single text files under ~/.clawford/.
#
# Usage (on VPS):
#   bash ops/scripts/install-bwrap-allowlist.sh
#
# Override the default-file path for tests:
#   DEFAULT_FILE=/path/to/fixture.txt TARGET_FILE=/tmp/test-allowlist.txt \
#     bash ops/scripts/install-bwrap-allowlist.sh
set -euo pipefail

DEFAULT_FILE="${DEFAULT_FILE:-$(dirname "$0")/../bwrap-allowlist.default.txt}"
TARGET_FILE="${TARGET_FILE:-$HOME/.clawford/bwrap-allowlist.txt}"

if [[ ! -f "$DEFAULT_FILE" ]]; then
  echo "install-bwrap-allowlist: default file not found: $DEFAULT_FILE" >&2
  exit 1
fi

mkdir -p "$(dirname "$TARGET_FILE")"
touch "$TARGET_FILE"

TMP=$(mktemp "${TARGET_FILE}.XXXXXX")
trap 'rm -f "$TMP"' EXIT

# Preserve everything already on the target file (including any
# operator-added entries not in the default list).
cat "$TARGET_FILE" > "$TMP"

ADDED=0
while IFS= read -r line || [[ -n "$line" ]]; do
  # Strip trailing CR (Windows line endings from repo files).
  line="${line%$'\r'}"
  # Skip comments and blank lines (trim leading/trailing whitespace).
  trimmed="${line#"${line%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  case "$trimmed" in
    '#'*|'') continue ;;
  esac
  # Exact-line dedupe against what's already in the merged result.
  if ! grep -Fxq "$trimmed" "$TMP"; then
    printf '%s\n' "$trimmed" >> "$TMP"
    ADDED=$((ADDED + 1))
  fi
done < "$DEFAULT_FILE"

mv "$TMP" "$TARGET_FILE"
trap - EXIT

echo "install-bwrap-allowlist: added $ADDED entries → $TARGET_FILE"
