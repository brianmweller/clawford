#!/usr/bin/env bash
# Test fixture: minimal install-host-cron.sh with one DIRECT and a few
# CONTRACT entries. The cron-self-check parser only reads these arrays.

DIRECT_ENTRIES=(
  "*/5 * * * *|costco-token-refresh-host.sh|# costco-token-refresh-host"
  "0 12 * * *|morning-fleet-deliver-host.sh|# morning-fleet-deliver-host"
)

CONTRACT_ENTRIES=(
  "0 */6 * * *|linkedin-keepalive|/path/linkedin-keepalive.py|NEWSDIGEST_BOT_TOKEN|300"
  "*/5 * * * *|family-calendar-reminder-check|/path/reminder-check.py|FAMILYCAL_BOT_TOKEN|90"
  "30 10 * * *|shopping-delivery-digest|/path/delivery-digest.py|SHOPPING_BOT_TOKEN|900"
)
