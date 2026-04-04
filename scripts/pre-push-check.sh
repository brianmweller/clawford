#!/usr/bin/env bash
# Pre-push safety check — run before pushing to GitHub
# Usage: bash scripts/pre-push-check.sh
# Returns: 0 if clean, 1 if issues found (warnings only, does not block)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ISSUES=0

echo "=== Pre-Push Safety Check ==="
echo ""

# 1. Check for secrets in staged/committed changes
echo "Checking for secrets in diff..."
DIFF=$(cd "$REPO_ROOT" && git diff origin/master..HEAD 2>/dev/null || git diff HEAD~1..HEAD 2>/dev/null || echo "")

if echo "$DIFF" | grep -qiE '(sk-ant-|sk-[a-z]{2,}-|ghp_|gho_|AKIA[A-Z0-9]{16}|password\s*=\s*["\x27][^"\x27]+|bot_token\s*=|api_key\s*=)'; then
    echo "  ⚠️  POSSIBLE SECRET detected in diff"
    echo "$DIFF" | grep -niE '(sk-ant-|sk-[a-z]{2,}-|ghp_|gho_|AKIA|password\s*=|bot_token\s*=|api_key\s*=)' | head -5
    ISSUES=$((ISSUES + 1))
else
    echo "  ✅ No secrets detected"
fi

# 2. Check for .env files
echo "Checking for .env files..."
if cd "$REPO_ROOT" && git diff --name-only origin/master..HEAD 2>/dev/null | grep -qE '\.env$'; then
    echo "  ⚠️  .env file in commit — should be gitignored"
    ISSUES=$((ISSUES + 1))
else
    echo "  ✅ No .env files"
fi

# 3. Check for large files (>1MB)
echo "Checking for large files..."
LARGE_FILES=$(cd "$REPO_ROOT" && git diff --name-only origin/master..HEAD 2>/dev/null | while read -r f; do
    if [ -f "$REPO_ROOT/$f" ]; then
        SIZE=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
        if [ "$SIZE" -gt 1048576 ]; then
            echo "  $f ($(( SIZE / 1024 ))KB)"
        fi
    fi
done)

if [ -n "$LARGE_FILES" ]; then
    echo "  ⚠️  Large files (>1MB):"
    echo "$LARGE_FILES"
    ISSUES=$((ISSUES + 1))
else
    echo "  ✅ No large files"
fi

# 4. Check commit messages aren't empty
echo "Checking commit messages..."
EMPTY_MSGS=$(cd "$REPO_ROOT" && git log origin/master..HEAD --format="%H %s" 2>/dev/null | while read -r hash msg; do
    if [ -z "$msg" ]; then
        echo "  $hash (empty message)"
    fi
done)

if [ -n "$EMPTY_MSGS" ]; then
    echo "  ⚠️  Empty commit messages:"
    echo "$EMPTY_MSGS"
    ISSUES=$((ISSUES + 1))
else
    echo "  ✅ All commits have messages"
fi

# 5. Summary
echo ""
if [ "$ISSUES" -eq 0 ]; then
    echo "✅ Pre-push check passed. Safe to push."
    exit 0
else
    echo "⚠️  $ISSUES issue(s) found. Review before pushing."
    exit 1
fi
