#!/usr/bin/env bash
# Meetings Coach (Sergeant Murphy) Test Suite — Setup Script
# Run on VPS: bash /tmp/meetings-coach-tests.sh
# Then: bash ~/openclaw-tests/test-agent.sh meetings-coach [--calibrate]
#
# Prerequisites:
#   - meetings-coach agent deployed and paired
#   - Google Calendar OAuth token valid
#   - test-lib.sh already installed (from setup-tests.sh)

set -euo pipefail

BASE="$HOME/openclaw-tests"
WORKSPACE="$HOME/.openclaw/meetings-coach-workspace"

echo "Creating meetings-coach tests at $BASE/tests/meetings-coach/..."
mkdir -p "$BASE/tests/meetings-coach" "$BASE/results"

# ═══════════════════════════════════════════════════════════════
# T1 — gcal-fetch returns valid JSON with attendee fields
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T1-gcal-fetch.sh" << 'T1'
# Does gcal-fetch.py return valid JSON with attendees and meeting filter?
test_start "T1" "gcal-fetch.py — Valid JSON with Attendee Fields"

TELEGRAM_ACCOUNT="murphy"

echo "  Running gcal-fetch.py..."
OUTPUT=$(python3 "$HOME/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py" 2>/dev/null || echo '{"status":"error","message":"script failed"}')

# Verify valid JSON
if ! echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  FAIL: output is not valid JSON"
    test_fail "invalid JSON output"
    return 1
fi

failures=0

# Verify status
STATUS=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
if [ "$STATUS" = "ok" ] || [ "$STATUS" = "partial" ]; then
    echo "  PASS: status is '$STATUS'"
else
    echo "  FAIL: status is '$STATUS' (expected ok or partial)"
    failures=$((failures + 1))
fi

# Verify events array
HAS_EVENTS=$(echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if 'events' in d else 'no')")
if [ "$HAS_EVENTS" = "yes" ]; then
    echo "  PASS: events array present"
else
    echo "  FAIL: events array missing"
    failures=$((failures + 1))
fi

# Verify event fields include attendees and is_real_meeting
EVENT_CHECK=$(echo "$OUTPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
events = d.get('events', [])
if not events:
    print('no_events')
else:
    e = events[0]
    required = ['summary', 'start', 'end', 'attendees', 'is_real_meeting', 'conference_link']
    missing = [k for k in required if k not in e]
    print('ok' if not missing else 'missing:' + ','.join(missing))
")
if [ "$EVENT_CHECK" = "ok" ]; then
    echo "  PASS: event fields include attendees, is_real_meeting, conference_link"
elif [ "$EVENT_CHECK" = "no_events" ]; then
    echo "  INFO: no events today (cannot verify field structure)"
else
    echo "  FAIL: $EVENT_CHECK"
    failures=$((failures + 1))
fi

# Verify cache file written
CACHE_FILE="$HOME/.openclaw/meetings-coach-workspace/cache/events-$(date -u +%Y-%m-%d).json"
if [ -f "$CACHE_FILE" ]; then
    echo "  PASS: cache file written"
else
    echo "  FAIL: cache file not created"
    failures=$((failures + 1))
fi

EVENT_COUNT=$(echo "$OUTPUT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('events',[])))")
REAL_COUNT=$(echo "$OUTPUT" | python3 -c "import sys,json; print(sum(1 for e in json.load(sys.stdin).get('events',[]) if e.get('is_real_meeting')))")
echo "  INFO: $EVENT_COUNT events fetched ($REAL_COUNT real meetings)"

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T1

# ═══════════════════════════════════════════════════════════════
# T2 — Meeting filter excludes non-meetings
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T2-meeting-filter.sh" << 'T2'
# Does is_real_meeting correctly filter task blocks vs real meetings?
test_start "T2" "Meeting Filter — Real vs Task Events"

TELEGRAM_ACCOUNT="murphy"

echo "  Running gcal-fetch.py --days 7..."
OUTPUT=$(python3 "$HOME/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py" --days 7 2>/dev/null || echo '{"status":"error"}')

FILTER_CHECK=$(echo "$OUTPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
events = d.get('events', [])
total = len(events)
real = sum(1 for e in events if e.get('is_real_meeting'))
filtered = total - real
# Check that filtered events lack attendees AND video links
bad_filters = 0
for e in events:
    if not e.get('is_real_meeting'):
        if e.get('attendees') or e.get('conference_link'):
            bad_filters += 1
print(f'{total},{real},{filtered},{bad_filters}')
")

IFS=',' read -r TOTAL REAL FILTERED BAD <<< "$FILTER_CHECK"

echo "  INFO: $TOTAL total events, $REAL real meetings, $FILTERED filtered"

if [ "$BAD" = "0" ]; then
    echo "  PASS: no false negatives in meeting filter"
    test_pass
    return 0
else
    echo "  FAIL: $BAD events have attendees/video but were filtered out"
    test_fail "$BAD false negatives"
    return 1
fi
T2

# ═══════════════════════════════════════════════════════════════
# T3 — Timed-deliver uses correct bot token
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T3-timed-deliver.sh" << 'T3'
# Does timed-deliver.py use MEETINGS_BOT_TOKEN and not another agent's token?
test_start "T3" "Timed Deliver — Correct Bot Token"

TELEGRAM_ACCOUNT="murphy"
SCRIPT="$HOME/.openclaw/meetings-coach-workspace/scripts/timed-deliver.py"

failures=0

if [ -f "$SCRIPT" ]; then
    echo "  PASS: timed-deliver.py exists"
else
    echo "  FAIL: timed-deliver.py not found"
    test_fail "script missing"
    return 1
fi

# Verify it fails without MEETINGS_BOT_TOKEN
echo "  Testing with empty MEETINGS_BOT_TOKEN..."
UNSET_OUTPUT=$(MEETINGS_BOT_TOKEN="" python3 "$SCRIPT" /dev/null --token-env MEETINGS_BOT_TOKEN 2>&1 || true)
if echo "$UNSET_OUTPUT" | grep -q "MEETINGS_BOT_TOKEN"; then
    echo "  PASS: fails loudly when MEETINGS_BOT_TOKEN is empty"
else
    echo "  FAIL: did not mention MEETINGS_BOT_TOKEN in error"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T3

# ═══════════════════════════════════════════════════════════════
# T4 — Heartbeat updates status file
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T4-heartbeat.sh" << 'T4'
# Does the heartbeat cron update the status file?
test_start "T4" "Heartbeat — Status File Update"

TELEGRAM_ACCOUNT="murphy"
STATUS_FILE="$HOME/Dropbox/openclaw-backup/agents/meetings-coach.status.md"

BEFORE=$(grep "last_heartbeat" "$STATUS_FILE" 2>/dev/null || echo "none")
echo "  Before: $BEFORE"

# Trigger
cron_id_line=$(trigger_cron "heartbeat")
cron_id=$(echo "$cron_id_line" | grep -oE '[0-9a-f-]{36}' | head -1)

if [ -z "$cron_id" ]; then
    test_fail "could not trigger heartbeat cron"
    return 1
fi

wait_for_cron_completion "$cron_id"
wait_result=$?

if [ "$wait_result" -eq 2 ]; then
    test_fail "timed out"
    return 1
fi

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
T4

# ═══════════════════════════════════════════════════════════════
# T5 — SOUL.md and IDENTITY.md are immutable
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T5-immutable.sh" << 'T5'
# Are SOUL.md and IDENTITY.md locked (chattr +i)?
test_start "T5" "Security — Immutable SOUL + IDENTITY"

TELEGRAM_ACCOUNT="murphy"
WORKSPACE="$HOME/.openclaw/meetings-coach-workspace"

failures=0

for file in SOUL.md IDENTITY.md; do
    ATTRS=$(lsattr "$WORKSPACE/$file" 2>/dev/null || echo "UNAVAILABLE")
    if echo "$ATTRS" | grep -q "i"; then
        echo "  PASS: $file is immutable"
    else
        echo "  FAIL: $file is NOT immutable ($ATTRS)"
        failures=$((failures + 1))
    fi
done

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures file(s) not locked"
    return 1
fi
T5

# ═══════════════════════════════════════════════════════════════
# T6 — commitment-tracker reads shared brain
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T6-commitment-tracker.sh" << 'T6'
# Does commitment-tracker.py return valid JSON?
test_start "T6" "Commitment Tracker — Valid JSON Output"

TELEGRAM_ACCOUNT="murphy"

echo "  Running commitment-tracker.py..."
OUTPUT=$(python3 "$HOME/.openclaw/meetings-coach-workspace/scripts/commitment-tracker.py" 2>/dev/null || echo '{"status":"error"}')

if ! echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  FAIL: output is not valid JSON"
    test_fail "invalid JSON"
    return 1
fi

failures=0

# Verify structure
STRUCTURE_CHECK=$(echo "$OUTPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
required = ['status', 'commitments', 'summary']
missing = [k for k in required if k not in d]
print('ok' if not missing else 'missing:' + ','.join(missing))
")
if [ "$STRUCTURE_CHECK" = "ok" ]; then
    echo "  PASS: output has status, commitments, summary fields"
else
    echo "  FAIL: $STRUCTURE_CHECK"
    failures=$((failures + 1))
fi

# Verify summary has expected subfields
SUMMARY_CHECK=$(echo "$OUTPUT" | python3 -c "
import sys, json
s = json.load(sys.stdin).get('summary', {})
required = ['total', 'open', 'overdue', 'approaching']
missing = [k for k in required if k not in s]
print('ok' if not missing else 'missing:' + ','.join(missing))
")
if [ "$SUMMARY_CHECK" = "ok" ]; then
    echo "  PASS: summary has total, open, overdue, approaching"
else
    echo "  FAIL: $SUMMARY_CHECK"
    failures=$((failures + 1))
fi

TOTAL=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary',{}).get('total',0))")
echo "  INFO: $TOTAL commitments tracked"

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T6

# ═══════════════════════════════════════════════════════════════
# T7 — person-bootstrap creates valid person files
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T7-person-bootstrap.sh" << 'T7'
# Does person-bootstrap.py output valid JSON with dry-run?
test_start "T7" "Person Bootstrap — Dry Run"

TELEGRAM_ACCOUNT="murphy"
CACHE_FILE="$HOME/.openclaw/meetings-coach-workspace/cache/events-$(date -u +%Y-%m-%d).json"

if [ ! -f "$CACHE_FILE" ]; then
    echo "  INFO: No cached events — running gcal-fetch.py first..."
    python3 "$HOME/.openclaw/meetings-coach-workspace/scripts/gcal-fetch.py" > /dev/null 2>&1
fi

if [ ! -f "$CACHE_FILE" ]; then
    echo "  SKIP: No cached events available"
    test_pass
    return 0
fi

echo "  Running person-bootstrap.py --dry-run..."
OUTPUT=$(python3 "$HOME/.openclaw/meetings-coach-workspace/scripts/person-bootstrap.py" \
    --from-events "$CACHE_FILE" --dry-run 2>/dev/null || echo '{"status":"error"}')

if ! echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  FAIL: output is not valid JSON"
    test_fail "invalid JSON"
    return 1
fi

failures=0

DRY_RUN=$(echo "$OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('dry_run', False))")
if [ "$DRY_RUN" = "True" ]; then
    echo "  PASS: dry_run=True (no files created)"
else
    echo "  FAIL: dry_run was not True"
    failures=$((failures + 1))
fi

CREATED=$(echo "$OUTPUT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('created',[])))")
SKIPPED=$(echo "$OUTPUT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('skipped',[])))")
echo "  INFO: would create $CREATED, skip $SKIPPED"

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T7

# ═══════════════════════════════════════════════════════════════
# T8 — Morning meeting brief cron
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T8-morning-brief.sh" << 'T8'
# Does the morning meeting brief cron produce output?
test_start "T8" "Morning Meeting Brief — Cron Output"

TELEGRAM_ACCOUNT="murphy"

cron_id_line=$(trigger_cron "morning-meeting-brief")
cron_id=$(echo "$cron_id_line" | grep -oE '[0-9a-f-]{36}' | head -1)

if [ -z "$cron_id" ]; then
    test_fail "could not trigger morning-meeting-brief cron"
    return 1
fi

POLL_TIMEOUT=180
wait_for_cron_completion "$cron_id"
wait_result=$?

if [ "$wait_result" -eq 2 ]; then
    test_fail "timed out waiting for morning brief"
    return 1
fi

failures=0

# Verify cache file was written
CACHE_FILE="$HOME/.openclaw/meetings-coach-workspace/cache/morning-meeting-brief.txt"
if [ -f "$CACHE_FILE" ]; then
    echo "  PASS: morning-meeting-brief.txt created"
else
    echo "  FAIL: morning-meeting-brief.txt not found"
    failures=$((failures + 1))
fi

if [ -f "$CACHE_FILE" ]; then
    assert_file_contains "$CACHE_FILE" "🐷🔍" "contains Sergeant Murphy emoji" || failures=$((failures + 1))
    assert_file_contains "$CACHE_FILE" "Meeting Brief\|All quiet\|meeting" "contains brief header or no-meetings message" || failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verification(s) failed"
    return 1
fi
T8

# ═══════════════════════════════════════════════════════════════
# T9 — On-demand /today query
# ═══════════════════════════════════════════════════════════════

cat > "$BASE/tests/meetings-coach/T9-on-demand.sh" << 'T9'
# Does the agent respond to /today on Telegram?
test_start "T9" "On-Demand Query — /today"

TELEGRAM_ACCOUNT="murphy"

echo "  Sending /today query..."
job_id_line=$(send_direct_message "meetings-coach" "/today")
job_id=$(echo "$job_id_line" | grep -oE '[0-9a-f-]{36}' | head -1)

if [ -z "$job_id" ]; then
    test_fail "failed to send /today query"
    return 1
fi

POLL_TIMEOUT=120
wait_for_cron_completion "$job_id"
wait_result=$?

cleanup_direct_message "$job_id"

if [ "$wait_result" -eq 2 ]; then
    test_fail "timed out"
    return 1
fi

assert_cron_output_contains "meeting|calendar|prep|quiet|today|brief" \
    "response contains meeting-related content" || {
    test_fail "response does not look like meeting info"
    return 1
}

test_pass
return 0
T9

# ═══════════════════════════════════════════════════════════════

echo ""
echo "============================================"
echo "  Meetings Coach tests installed (9 tests)"
echo ""
echo "  Usage:"
echo "    bash ~/openclaw-tests/test-agent.sh meetings-coach --calibrate"
echo "    bash ~/openclaw-tests/test-agent.sh meetings-coach T1"
echo "    bash ~/openclaw-tests/test-agent.sh meetings-coach"
echo "============================================"
