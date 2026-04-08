# Does chat-parse-schedule.py return valid JSON?
test_start "T13" "Chat Parse — WhatsApp Message Extraction"

DOCKER="docker compose -f $HOME/openclaw/docker-compose.yml exec -T openclaw-gateway"
SCRIPT="/home/node/.openclaw/family-calendar-workspace/scripts/chat-parse-schedule.py"

failures=0

# Test 1: script exists
$DOCKER test -f "$SCRIPT" && echo "  PASS: chat-parse-schedule.py exists" || {
    echo "  FAIL: chat-parse-schedule.py not found"
    test_fail "script missing"
    return 1
}

# Test 2: returns valid JSON (even with no messages)
echo "  Running chat-parse-schedule.py..."
OUTPUT=$($DOCKER python3 "$SCRIPT" 2>&1)

if echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  PASS: returns valid JSON"
else
    echo "  FAIL: output is not valid JSON"
    echo "  Output: $OUTPUT"
    failures=$((failures + 1))
fi

# Test 3: output is an array
IS_ARRAY=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if isinstance(d,list) else 'no')" 2>/dev/null || echo "no")
if [ "$IS_ARRAY" = "yes" ]; then
    MSG_COUNT=$(echo "$OUTPUT" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
    echo "  PASS: output is an array ($MSG_COUNT messages)"
else
    echo "  FAIL: output is not an array"
    failures=$((failures + 1))
fi

# Test 4: no openai import in script (agent LLM does parsing, not script)
echo "  Checking script has no direct LLM calls..."
HAS_OPENAI=$($DOCKER sh -c "grep -c 'import openai' $SCRIPT || true" 2>/dev/null)
if [ "$HAS_OPENAI" = "0" ]; then
    echo "  PASS: no openai import (correct — agent LLM does parsing)"
else
    echo "  FAIL: script imports openai directly"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
