# Does wechat-compose.py produce Chinese text?
test_start "T13" "WeChat Compose — Chinese Message Output"

DOCKER="docker compose -f $HOME/openclaw/docker-compose.yml exec -T openclaw-gateway"
SCRIPT="/home/node/.openclaw/family-calendar-workspace/scripts/wechat-compose.py"

failures=0

# Test 1: script exists
$DOCKER test -f "$SCRIPT" && echo "  PASS: wechat-compose.py exists" || {
    echo "  FAIL: wechat-compose.py not found"
    test_fail "script missing"
    return 1
}

# Test 2: compose with --text produces valid JSON with Chinese
echo "  Testing compose with --text..."
OUTPUT=$($DOCKER python3 "$SCRIPT" --mode group --text "Avery has a ballet recital on May 3rd at 2pm" 2>&1)

if echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  PASS: returns valid JSON"
else
    echo "  FAIL: output is not valid JSON"
    echo "  Output: $OUTPUT"
    failures=$((failures + 1))
fi

# Test 3: output contains chinese field
HAS_CHINESE=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('chinese') else 'no')" 2>/dev/null || echo "no")
if [ "$HAS_CHINESE" = "yes" ]; then
    CHINESE=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('chinese','')[:50])" 2>/dev/null)
    echo "  PASS: chinese field present ($CHINESE...)"
else
    echo "  FAIL: no chinese field in output"
    failures=$((failures + 1))
fi

# Test 4: output contains english_summary
HAS_SUMMARY=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('english_summary') else 'no')" 2>/dev/null || echo "no")
if [ "$HAS_SUMMARY" = "yes" ]; then
    echo "  PASS: english_summary field present"
else
    echo "  FAIL: no english_summary field"
    failures=$((failures + 1))
fi

# Test 5: mode is correct
MODE=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mode',''))" 2>/dev/null || echo "")
if [ "$MODE" = "group" ]; then
    echo "  PASS: mode is group"
else
    echo "  FAIL: mode is '$MODE' (expected group)"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
