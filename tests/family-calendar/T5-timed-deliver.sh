# Does timed-deliver.py use FAMILYCAL_BOT_TOKEN and not another agent's token?
test_start "T5" "Timed Deliver — Correct Bot Token"

TELEGRAM_ACCOUNT="familycal"
DOCKER="docker compose -f $HOME/openclaw/docker-compose.yml exec -T openclaw-gateway"
SCRIPT="/home/node/.openclaw/family-calendar-workspace/scripts/timed-deliver.py"

failures=0

# Verify script exists
$DOCKER test -f "$SCRIPT" && echo "  PASS: timed-deliver.py exists" || {
    echo "  FAIL: timed-deliver.py not found"
    test_fail "script missing"
    return 1
}

# Verify it fails without FAMILYCAL_BOT_TOKEN
echo "  Testing with empty FAMILYCAL_BOT_TOKEN..."
UNSET_OUTPUT=$($DOCKER env FAMILYCAL_BOT_TOKEN="" python3 "$SCRIPT" /dev/null --token-env FAMILYCAL_BOT_TOKEN 2>&1 || true)
if echo "$UNSET_OUTPUT" | grep -q "FAMILYCAL_BOT_TOKEN"; then
    echo "  PASS: fails loudly when FAMILYCAL_BOT_TOKEN is empty"
else
    echo "  FAIL: did not mention FAMILYCAL_BOT_TOKEN in error"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
