# Does gcal-write.py move events correctly?
test_start "T11" "Calendar Write — Move Event"

TELEGRAM_ACCOUNT="familycal"
DOCKER="docker compose -f $HOME/openclaw/docker-compose.yml exec -T openclaw-gateway"
SCRIPT="/home/node/.openclaw/family-calendar-workspace/scripts/gcal-write.py"

failures=0

# Create a test event
echo "  Creating test event..."
CREATE_OUT=$($DOCKER python3 $SCRIPT create --calendar-id Sam --summary "__TEST__ move test" --start "2026-04-20T10:00" --end "2026-04-20T10:30" --confirm 2>&1)
EVENT_ID=$(echo "$CREATE_OUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('event_id',''))" 2>/dev/null || echo "")

if [ -z "$EVENT_ID" ]; then
    echo "  FAIL: could not create test event"
    test_fail "setup failed"
    return 1
fi
echo "  Created: ${EVENT_ID:0:12}..."

# Move it
echo "  Moving event to new time..."
MOVE_OUT=$($DOCKER python3 $SCRIPT move --calendar-id Sam --event-id "$EVENT_ID" --new-start "2026-04-20T14:00" --new-end "2026-04-20T14:30" --confirm 2>&1)
MOVE_STATUS=$(echo "$MOVE_OUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
NEW_START=$(echo "$MOVE_OUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('new_start',''))" 2>/dev/null || echo "")

if [ "$MOVE_STATUS" = "ok" ]; then
    echo "  PASS: move returned status ok"
else
    echo "  FAIL: move status is '$MOVE_STATUS'"
    failures=$((failures + 1))
fi

if echo "$NEW_START" | grep -q "14:00"; then
    echo "  PASS: new start time is 14:00"
else
    echo "  FAIL: new start time is '$NEW_START'"
    failures=$((failures + 1))
fi

# Verify by fetching
echo "  Verifying via gcal-fetch..."
FETCH_OUT=$($DOCKER python3 /home/node/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --date 2026-04-20 2>&1)
HAS_MOVED=$(echo "$FETCH_OUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for e in d.get('events', []):
    if '__TEST__ move test' in e.get('summary', '') and '14:00' in e.get('start', ''):
        print('yes')
        break
else:
    print('no')
" 2>/dev/null || echo "no")

if [ "$HAS_MOVED" = "yes" ]; then
    echo "  PASS: moved event verified via fetch"
else
    echo "  FAIL: moved event not found at new time"
    failures=$((failures + 1))
fi

# Cleanup
echo "  Removing test event..."
$DOCKER python3 $SCRIPT remove --calendar-id Sam --event-id "$EVENT_ID" --confirm > /dev/null 2>&1

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
