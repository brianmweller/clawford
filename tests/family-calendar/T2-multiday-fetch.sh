# Does gcal-fetch.py handle --days 2 for today + tomorrow?
test_start "T2" "gcal-fetch.py — Multi-Day Fetch --days 2"

TELEGRAM_ACCOUNT="familycal"
DOCKER="docker compose -f $HOME/openclaw/docker-compose.yml exec -T openclaw-gateway"

echo "  Running gcal-fetch.py --days 2..."
OUTPUT=$($DOCKER python3 /home/node/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py --days 2 2>/dev/null || echo '{"status":"error"}')

failures=0

STATUS=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
if [ "$STATUS" = "ok" ] || [ "$STATUS" = "partial" ]; then
    echo "  PASS: status is $STATUS"
else
    echo "  FAIL: status is $STATUS"
    failures=$((failures + 1))
fi

DAYS=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('days',0))")
if [ "$DAYS" = "2" ]; then
    echo "  PASS: days field is 2"
else
    echo "  FAIL: days field is $DAYS - expected 2"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
