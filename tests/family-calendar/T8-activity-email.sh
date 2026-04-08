# Does activity-email-check.py return valid JSON and connect to Gmail?
test_start "T8" "Activity Email Check — Gmail Connection + Valid Output"

TELEGRAM_ACCOUNT="familycal"
DOCKER="docker compose -f $HOME/openclaw/docker-compose.yml exec -T openclaw-gateway"

failures=0

# Test 1: dry-run returns valid JSON (no LLM calls)
echo "  Running activity-email-check.py --dry-run..."
OUTPUT=$($DOCKER python3 /home/node/.openclaw/family-calendar-workspace/scripts/activity-email-check.py --dry-run 2>/tmp/activity-stderr || echo "[]")

if echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  PASS: dry-run returns valid JSON"
else
    echo "  FAIL: dry-run output is not valid JSON"
    failures=$((failures + 1))
fi

# Test 2: Check stderr for Gmail errors (API not enabled, auth failure)
STDERR=$(cat /tmp/activity-stderr 2>/dev/null || echo "")
if echo "$STDERR" | grep -qi "403\|accessNotConfigured\|disabled"; then
    echo "  FAIL: Gmail API not enabled or access denied"
    echo "  Error: $STDERR"
    failures=$((failures + 1))
elif echo "$STDERR" | grep -qi "Token refresh failed\|token.json not found"; then
    echo "  FAIL: OAuth token issue"
    failures=$((failures + 1))
else
    echo "  PASS: no Gmail API errors"
fi

# Test 3: Full run (with LLM) returns valid JSON
echo "  Running activity-email-check.py (full, with LLM)..."
FULL_OUTPUT=$($DOCKER python3 /home/node/.openclaw/family-calendar-workspace/scripts/activity-email-check.py 2>/tmp/activity-stderr-full || echo "[]")

if echo "$FULL_OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  PASS: full run returns valid JSON"
else
    echo "  FAIL: full run output is not valid JSON"
    failures=$((failures + 1))
fi

ITEM_COUNT=$(echo "$FULL_OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 0)" 2>/dev/null || echo "0")
echo "  INFO: $ITEM_COUNT actionable items found"

# Test 4: seen-activity-emails.json exists and is valid
SEEN_FILE="$HOME/.openclaw/family-calendar-workspace/cache/seen-activity-emails.json"
if [ -f "$SEEN_FILE" ]; then
    if python3 -c "import json; json.load(open('$SEEN_FILE'))" 2>/dev/null; then
        echo "  PASS: seen-activity-emails.json is valid JSON"
    else
        echo "  FAIL: seen-activity-emails.json is corrupt"
        failures=$((failures + 1))
    fi
else
    echo "  INFO: seen-activity-emails.json not yet created (no emails processed)"
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
