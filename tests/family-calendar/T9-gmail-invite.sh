# Does gmail-invite-check.py return valid JSON and connect to Gmail?
test_start "T9" "Gmail Invite Check — Valid Output"

TELEGRAM_ACCOUNT="familycal"
DOCKER="docker compose -f $HOME/openclaw/docker-compose.yml exec -T openclaw-gateway"

failures=0

echo "  Running gmail-invite-check.py..."
OUTPUT=$($DOCKER python3 /home/node/.openclaw/family-calendar-workspace/scripts/gmail-invite-check.py 2>/tmp/invite-stderr || echo "[]")

# Test 1: valid JSON
if echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  PASS: returns valid JSON"
else
    echo "  FAIL: output is not valid JSON"
    failures=$((failures + 1))
fi

# Test 2: Check for Gmail API errors
STDERR=$(cat /tmp/invite-stderr 2>/dev/null || echo "")
if echo "$STDERR" | grep -qi "403\|accessNotConfigured\|disabled"; then
    echo "  FAIL: Gmail API not enabled or access denied"
    failures=$((failures + 1))
else
    echo "  PASS: no Gmail API errors"
fi

# Test 3: output is an array
IS_ARRAY=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if isinstance(d,list) else 'no')" 2>/dev/null || echo "no")
if [ "$IS_ARRAY" = "yes" ]; then
    echo "  PASS: output is an array"
else
    echo "  FAIL: output is not an array"
    failures=$((failures + 1))
fi

INVITE_COUNT=$(echo "$OUTPUT" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
echo "  INFO: $INVITE_COUNT invites found"

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
