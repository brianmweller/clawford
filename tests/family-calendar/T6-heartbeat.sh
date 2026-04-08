# Does the heartbeat cron update the status file?
test_start "T6" "Heartbeat — Status File Update"

TELEGRAM_ACCOUNT="familycal"
STATUS_FILE="$HOME/Dropbox/openclaw-backup/agents/family-calendar.status.md"

# Record current heartbeat
BEFORE=$(grep "last_heartbeat" "$STATUS_FILE" 2>/dev/null || echo "none")
echo "  Before: $BEFORE"

# Trigger
cron_id_line=$(trigger_cron "heartbeat")
cron_id=$(echo "$cron_id_line" | grep -oE '[0-9a-f-]{36}' | head -1)

if [ -z "$cron_id" ]; then
    test_fail "could not trigger heartbeat cron"
    return 1
fi

# Wait
wait_for_cron_completion "$cron_id"
wait_result=$?

if [ "$wait_result" -eq 2 ]; then
    test_fail "timed out"
    return 1
fi

# Verify heartbeat was updated
AFTER=$(grep "last_heartbeat" "$STATUS_FILE" 2>/dev/null || echo "none")
echo "  After: $AFTER"

if [ "$BEFORE" != "$AFTER" ]; then
    echo "  PASS: heartbeat timestamp changed"
    test_pass
    return 0
else
    echo "  FAIL: heartbeat timestamp unchanged"
    test_fail "heartbeat not updated"
    return 1
fi
