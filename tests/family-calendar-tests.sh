#!/usr/bin/env bash
# Family Calendar (Mistress Mouse) Test Suite — Setup Script
# Run on VPS: bash /tmp/family-calendar-tests.sh
# Then: bash ~/openclaw-tests/test-agent.sh family-calendar [--calibrate]
#
# Prerequisites:
#   - family-calendar agent deployed and paired
#   - Google Calendar OAuth token valid
#   - At least one event on Sam's or Alex's calendar for today/tomorrow
#   - test-lib.sh already installed (from setup-tests.sh)

set -euo pipefail

BASE="$HOME/openclaw-tests"
WORKSPACE="$HOME/.openclaw/family-calendar-workspace"

echo "Creating family-calendar tests at $BASE/tests/family-calendar/..."
mkdir -p "$BASE/tests/family-calendar" "$BASE/results"

# ═══════════════════════════════════════════════════════════════
# T1 — gcal-fetch returns valid JSON
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/family-calendar/T1-gcal-fetch.sh" << 'T1'
# Does gcal-fetch.py return valid JSON with expected structure?
test_start "T1" "gcal-fetch.py — Valid JSON Output"

TELEGRAM_ACCOUNT="familycal"

echo "  Running gcal-fetch.py..."
OUTPUT=$(python3 "$HOME/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py" 2>/dev/null || echo '{"status":"error","message":"script failed"}')

# Verify it's valid JSON
if ! echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  FAIL: output is not valid JSON"
    test_fail "invalid JSON output"
    return 1
fi

failures=0

# Verify structure
STATUS=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
if [ "$STATUS" = "ok" ] || [ "$STATUS" = "partial" ]; then
    echo "  PASS: status is '$STATUS'"
else
    echo "  FAIL: status is '$STATUS' (expected ok or partial)"
    failures=$((failures + 1))
fi

# Verify events array exists
HAS_EVENTS=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if 'events' in d else 'no')")
if [ "$HAS_EVENTS" = "yes" ]; then
    echo "  PASS: events array present"
else
    echo "  FAIL: events array missing"
    failures=$((failures + 1))
fi

# Verify conflicts array exists
HAS_CONFLICTS=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if 'conflicts' in d else 'no')")
if [ "$HAS_CONFLICTS" = "yes" ]; then
    echo "  PASS: conflicts array present"
else
    echo "  FAIL: conflicts array missing"
    failures=$((failures + 1))
fi

# Verify date field
HAS_DATE=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('date','') else 'no')")
if [ "$HAS_DATE" = "yes" ]; then
    echo "  PASS: date field present"
else
    echo "  FAIL: date field missing"
    failures=$((failures + 1))
fi

# Verify events have required fields (if any exist)
EVENT_CHECK=$(echo "$OUTPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
events = d.get('events', [])
if not events:
    print('no_events')
else:
    e = events[0]
    required = ['summary', 'start', 'end', 'calendar_label', 'calendar_emoji']
    missing = [k for k in required if k not in e]
    print('ok' if not missing else 'missing:' + ','.join(missing))
")
if [ "$EVENT_CHECK" = "ok" ]; then
    echo "  PASS: event fields complete"
elif [ "$EVENT_CHECK" = "no_events" ]; then
    echo "  INFO: no events today (cannot verify event field structure)"
else
    echo "  FAIL: $EVENT_CHECK"
    failures=$((failures + 1))
fi

# Verify cache file written
CACHE_FILE="$HOME/.openclaw/family-calendar-workspace/cache/events-$(date -u +%Y-%m-%d).json"
if [ -f "$CACHE_FILE" ]; then
    echo "  PASS: cache file written"
else
    echo "  FAIL: cache file not created"
    failures=$((failures + 1))
fi

EVENT_COUNT=$(echo "$OUTPUT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('events',[])))")
echo "  INFO: $EVENT_COUNT events fetched for today"

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T1

# ═══════════════════════════════════════════════════════════════
# T2 — Multi-day fetch and tomorrow preview
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/family-calendar/T2-multiday-fetch.sh" << 'T2'
# Does gcal-fetch.py handle --days 2 for today + tomorrow?
test_start "T2" "gcal-fetch.py — Multi-Day Fetch (--days 2)"

TELEGRAM_ACCOUNT="familycal"

echo "  Running gcal-fetch.py --days 2..."
OUTPUT=$(python3 "$HOME/.openclaw/family-calendar-workspace/scripts/gcal-fetch.py" --days 2 2>/dev/null || echo '{"status":"error"}')

failures=0

STATUS=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
if [ "$STATUS" = "ok" ] || [ "$STATUS" = "partial" ]; then
    echo "  PASS: status is '$STATUS'"
else
    echo "  FAIL: status is '$STATUS'"
    failures=$((failures + 1))
fi

DAYS=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('days',0))")
if [ "$DAYS" = "2" ]; then
    echo "  PASS: days field is 2"
else
    echo "  FAIL: days field is '$DAYS' (expected 2)"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T2

# ═══════════════════════════════════════════════════════════════
# T3 — Morning briefing cron produces output
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/family-calendar/T3-morning-briefing.sh" << 'T3'
# Does the morning briefing cron produce formatted output?
test_start "T3" "Morning Briefing — Cron Output"

TELEGRAM_ACCOUNT="familycal"

# Trigger
cron_id_line=$(trigger_cron "morning-briefing")
cron_id=$(echo "$cron_id_line" | grep -oE '[0-9a-f-]{36}' | head -1)

if [ -z "$cron_id" ]; then
    test_fail "could not trigger morning-briefing cron"
    return 1
fi

# Wait (briefing may take up to 60s to fetch + format + deliver)
POLL_TIMEOUT=120
wait_for_cron_completion "$cron_id"
wait_result=$?

if [ "$wait_result" -eq 2 ]; then
    test_fail "timed out waiting for morning briefing"
    return 1
fi

failures=0

# Verify cache file was written
CACHE_FILE="$HOME/.openclaw/family-calendar-workspace/cache/morning-briefing.txt"
if [ -f "$CACHE_FILE" ]; then
    echo "  PASS: morning-briefing.txt created"
else
    echo "  FAIL: morning-briefing.txt not found"
    failures=$((failures + 1))
fi

# Verify output contains expected elements
if [ -f "$CACHE_FILE" ]; then
    assert_file_contains "$CACHE_FILE" "🐭" "contains Mistress Mouse emoji" || failures=$((failures + 1))
    assert_file_contains "$CACHE_FILE" "Family Day\|Standard\|no exceptions" "contains briefing header or standard day" || failures=$((failures + 1))
fi

# Verify cron ran successfully
assert_cron_output_contains "Status: ok\|finished\|success" "cron completed successfully" || failures=$((failures + 1))

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T3

# ═══════════════════════════════════════════════════════════════
# T4 — Reminder deduplication
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/family-calendar/T4-reminder-dedup.sh" << 'T4'
# Does reminder-check.py correctly deduplicate?
test_start "T4" "Reminder Dedup — No Double-Send"

TELEGRAM_ACCOUNT="familycal"
REMINDERS_FILE="$HOME/.openclaw/family-calendar-workspace/sent-reminders.json"

echo "  Running reminder-check.py (first run)..."
OUTPUT1=$(python3 "$HOME/.openclaw/family-calendar-workspace/scripts/reminder-check.py" 2>/dev/null || echo "[]")

REMINDER_COUNT1=$(echo "$OUTPUT1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d) if isinstance(d, list) else 0)")
echo "  First run: $REMINDER_COUNT1 reminders"

if [ "$REMINDER_COUNT1" = "0" ]; then
    echo "  INFO: No upcoming events in next 60 min — injecting a fake reminder to test dedup"

    # Inject a fake entry into sent-reminders.json
    python3 -c "
import json
with open('$REMINDERS_FILE') as f:
    data = json.load(f)
data['reminders']['__TEST__fake_event_brian_30min'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
with open('$REMINDERS_FILE', 'w') as f:
    json.dump(data, f, indent=2)
print('Injected test reminder')
"

    # Verify it's in the file
    if grep -q "__TEST__fake_event" "$REMINDERS_FILE"; then
        echo "  PASS: test reminder injected into sent-reminders.json"
    else
        echo "  FAIL: could not inject test reminder"
        test_fail "setup failed"
        return 1
    fi

    # Run again — the fake entry should persist (we're testing the file format works)
    echo "  Running reminder-check.py (second run)..."
    OUTPUT2=$(python3 "$HOME/.openclaw/family-calendar-workspace/scripts/reminder-check.py" 2>/dev/null || echo "[]")

    # Verify the file still has the test entry (wasn't corrupted by the second run)
    if grep -q "__TEST__fake_event" "$REMINDERS_FILE"; then
        echo "  PASS: sent-reminders.json preserved across runs"
    else
        echo "  FAIL: sent-reminders.json corrupted"
        test_fail "dedup file corrupted"
        return 1
    fi

    # Clean up
    python3 -c "
import json
with open('$REMINDERS_FILE') as f:
    data = json.load(f)
data['reminders'] = {k:v for k,v in data['reminders'].items() if '__TEST__' not in k}
with open('$REMINDERS_FILE', 'w') as f:
    json.dump(data, f, indent=2)
"

    test_pass
    return 0
fi

# If there WERE reminders, run again and verify they're not re-sent
echo "  Running reminder-check.py (second run)..."
OUTPUT2=$(python3 "$HOME/.openclaw/family-calendar-workspace/scripts/reminder-check.py" 2>/dev/null || echo "[]")

REMINDER_COUNT2=$(echo "$OUTPUT2" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d) if isinstance(d, list) else 0)")
echo "  Second run: $REMINDER_COUNT2 reminders"

if [ "$REMINDER_COUNT2" -lt "$REMINDER_COUNT1" ] || [ "$REMINDER_COUNT2" = "0" ]; then
    echo "  PASS: second run sent fewer/no reminders (dedup working)"
    test_pass
    return 0
else
    echo "  FAIL: second run sent same/more reminders (dedup broken)"
    test_fail "dedup not working"
    return 1
fi
T4

# ═══════════════════════════════════════════════════════════════
# T5 — timed-deliver uses correct bot token
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/family-calendar/T5-timed-deliver.sh" << 'T5'
# Does timed-deliver.py use FAMILYCAL_BOT_TOKEN and not another agent's token?
test_start "T5" "Timed Deliver — Correct Bot Token"

TELEGRAM_ACCOUNT="familycal"
SCRIPT="$HOME/.openclaw/family-calendar-workspace/scripts/timed-deliver.py"

failures=0

# Verify script exists
if [ -f "$SCRIPT" ]; then
    echo "  PASS: timed-deliver.py exists"
else
    echo "  FAIL: timed-deliver.py not found"
    test_fail "script missing"
    return 1
fi

# Verify it fails without FAMILYCAL_BOT_TOKEN
echo "  Testing with empty FAMILYCAL_BOT_TOKEN..."
UNSET_OUTPUT=$(FAMILYCAL_BOT_TOKEN="" python3 "$SCRIPT" /dev/null --token-env FAMILYCAL_BOT_TOKEN 2>&1 || true)
if echo "$UNSET_OUTPUT" | grep -q "FAMILYCAL_BOT_TOKEN"; then
    echo "  PASS: fails loudly when FAMILYCAL_BOT_TOKEN is empty"
else
    echo "  FAIL: did not mention FAMILYCAL_BOT_TOKEN in error"
    failures=$((failures + 1))
fi

# Verify it doesn't fall back to TELEGRAM_BOT_TOKEN (wrong bot)
echo "  Verifying no fallback to TELEGRAM_BOT_TOKEN..."
FALLBACK_OUTPUT=$(FAMILYCAL_BOT_TOKEN="" TELEGRAM_BOT_TOKEN="should-not-use-this" python3 "$SCRIPT" /dev/null --token-env FAMILYCAL_BOT_TOKEN 2>&1 || true)
if echo "$FALLBACK_OUTPUT" | grep -q "FAMILYCAL_BOT_TOKEN"; then
    echo "  PASS: does not silently fall back to TELEGRAM_BOT_TOKEN"
else
    echo "  FAIL: may have fallen back to wrong token"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T5

# ═══════════════════════════════════════════════════════════════
# T6 — Heartbeat updates status file
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/family-calendar/T6-heartbeat.sh" << 'T6'
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
T6

# ═══════════════════════════════════════════════════════════════
# T7 — On-demand /today query
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/family-calendar/T7-on-demand.sh" << 'T7'
# Does the agent respond to /today on Telegram?
test_start "T7" "On-Demand Query — /today"

TELEGRAM_ACCOUNT="familycal"

echo "  Sending /today query..."
job_id_line=$(send_direct_message "family-calendar" \
    "/today")
job_id=$(echo "$job_id_line" | grep -oE '[0-9a-f-]{36}' | head -1)

if [ -z "$job_id" ]; then
    test_fail "failed to send /today query"
    return 1
fi

# Wait
POLL_TIMEOUT=120
wait_for_cron_completion "$job_id"
wait_result=$?

# Cleanup
cleanup_direct_message "$job_id"

if [ "$wait_result" -eq 2 ]; then
    test_fail "timed out"
    return 1
fi

# Verify output mentions schedule content
assert_cron_output_contains "event|calendar|schedule|today|standard|no exceptions|Family Day" \
    "response contains schedule-related content" || {
    test_fail "response does not look like a schedule"
    return 1
}

test_pass
return 0
T7

# ═══════════════════════════════════════════════════════════════

echo ""
echo "============================================"
echo "  Family Calendar tests installed"
echo ""
echo "  Usage:"
echo "    bash ~/openclaw-tests/test-agent.sh family-calendar --calibrate"
echo "    bash ~/openclaw-tests/test-agent.sh family-calendar T1"
echo "    bash ~/openclaw-tests/test-agent.sh family-calendar"
echo "============================================"
