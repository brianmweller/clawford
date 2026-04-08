# Does WhatsApp delivery work to the family group?
test_start "T12" "WhatsApp — Channel Connected + Group Delivery"

failures=0

# Test 1: WhatsApp channel is linked
echo "  Checking WhatsApp channel status..."
WA_STATUS=$(cd ~/openclaw && docker compose exec -T openclaw-gateway openclaw channels status 2>&1 | grep "familycal-wa")
if echo "$WA_STATUS" | grep -q "linked.*running.*connected"; then
    echo "  PASS: WhatsApp familycal-wa is linked and connected"
else
    echo "  FAIL: WhatsApp familycal-wa not connected"
    echo "  Status: $WA_STATUS"
    failures=$((failures + 1))
fi

# Test 2: WhatsApp bound to family-calendar agent
echo "  Checking agent binding..."
BINDINGS=$(cd ~/openclaw && docker compose exec -T openclaw-gateway openclaw agents list --bindings 2>&1 | grep -A5 "family-calendar")
if echo "$BINDINGS" | grep -qi "whatsapp"; then
    echo "  PASS: WhatsApp bound to family-calendar"
else
    echo "  FAIL: WhatsApp not bound to family-calendar"
    failures=$((failures + 1))
fi

# Test 3: Group JID exists in Baileys store
echo "  Checking for group JID in Baileys store..."
GROUP_FILES=$(find ~/.openclaw/credentials/whatsapp/familycal-wa/ -name "*@g.us*" 2>/dev/null | head -1)
if [ -n "$GROUP_FILES" ]; then
    GROUP_JID=$(echo "$GROUP_FILES" | grep -oE '[0-9]+@g.us' | head -1)
    echo "  PASS: group JID found ($GROUP_JID)"
else
    echo "  FAIL: no group JID in Baileys store"
    failures=$((failures + 1))
fi

# Test 4: whatsapp-schedule-post cron exists
echo "  Checking whatsapp-schedule-post cron..."
WA_CRON=$(cd ~/openclaw && docker compose exec -T openclaw-gateway openclaw cron list 2>&1 | grep "whatsapp-schedule-post")
if [ -n "$WA_CRON" ]; then
    echo "  PASS: whatsapp-schedule-post cron registered"
else
    echo "  FAIL: whatsapp-schedule-post cron not found"
    failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
    test_pass
    return 0
else
    test_fail "$failures verifications failed"
    return 1
fi
