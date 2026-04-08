# Does gcal-write.py require --confirm and reject without it?
test_start "T10" "Calendar Write — Confirm Safety Gate"

TELEGRAM_ACCOUNT="familycal"
DOCKER="docker compose -f $HOME/openclaw/docker-compose.yml exec -T openclaw-gateway"
SCRIPT="/home/node/.openclaw/family-calendar-workspace/scripts/gcal-write.py"

failures=0

# Test 1: create WITHOUT --confirm should fail
echo "  Testing create without --confirm..."
OUTPUT=$($DOCKER python3 $SCRIPT create --calendar-id Sam --summary "__TEST__ should fail" --start "2026-04-20T10:00" --end "2026-04-20T10:30" 2>&1 || true)
if echo "$OUTPUT" | grep -qi "requires --confirm"; then
    echo "  PASS: create rejected without --confirm"
else
    echo "  FAIL: create did not require --confirm"
    echo "  Output: $OUTPUT"
    failures=$((failures + 1))
fi

# Test 2: preview-create works without --confirm
echo "  Testing preview-create..."
PREVIEW=$($DOCKER python3 $SCRIPT preview-create --calendar-id Sam --summary "__TEST__ preview" --start "2026-04-20T10:00" --end "2026-04-20T10:30" 2>&1)
PREVIEW_STATUS=$(echo "$PREVIEW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
if [ "$PREVIEW_STATUS" = "preview" ]; then
    echo "  PASS: preview-create returns status=preview"
else
    echo "  FAIL: preview-create status is '$PREVIEW_STATUS'"
    failures=$((failures + 1))
fi

# Test 3: create+remove round-trip WITH --confirm
echo "  Testing create with --confirm..."
CREATE_OUT=$($DOCKER python3 $SCRIPT create --calendar-id Sam --summary "__TEST__ write roundtrip" --start "2026-04-20T10:00" --end "2026-04-20T10:30" --confirm 2>&1)
EVENT_ID=$(echo "$CREATE_OUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('event_id',''))" 2>/dev/null || echo "")

if [ -n "$EVENT_ID" ]; then
    echo "  PASS: event created (ID: ${EVENT_ID:0:12}...)"

    # Remove it
    echo "  Removing test event..."
    REMOVE_OUT=$($DOCKER python3 $SCRIPT remove --calendar-id Sam --event-id "$EVENT_ID" --confirm 2>&1)
    REMOVE_STATUS=$(echo "$REMOVE_OUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")

    if [ "$REMOVE_STATUS" = "ok" ]; then
        echo "  PASS: test event removed"
    else
        echo "  FAIL: could not remove test event"
        echo "  Output: $REMOVE_OUT"
        failures=$((failures + 1))
    fi
else
    echo "  FAIL: create did not return event_id"
    echo "  Output: $CREATE_OUT"
    failures=$((failures + 1))
fi

# Test 4: audit log was written
echo "  Checking audit log..."
LOG_FILE="/home/node/.openclaw/family-calendar-workspace/logs/calendar-writes.jsonl"
LOG_CHECK=$($DOCKER sh -c "test -f $LOG_FILE && grep -c __TEST__ $LOG_FILE || echo 0" 2>/dev/null)
if [ "$LOG_CHECK" -gt 0 ] 2>/dev/null; then
    echo "  PASS: audit log has $LOG_CHECK test entries"
else
    echo "  FAIL: audit log missing or no test entries"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
