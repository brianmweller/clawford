# Huckle real-time Gmail triage — setup

One-time setup for the Pub/Sub pull pipeline that gives sub-minute
latency on Gmail inbounds. Layered on top of the existing 30-min
polling cron — the polling cron stays enabled as belt-and-suspenders.

## 1. GCP resources (one-time)

Google Cloud Console or gcloud. Run once per Gmail account; results
live in the GCP project that already hosts the operator's Gmail OAuth
credentials (Desktop app type, per `reference_google_oauth.md`).

```bash
PROJECT=$(gcloud config get-value project)   # or set explicitly
TOPIC=gmail-huckle-inbox
SUB=huckle-gmail-pull

gcloud services enable pubsub.googleapis.com

gcloud pubsub topics create "$TOPIC"

gcloud pubsub subscriptions create "$SUB" \
    --topic="$TOPIC" \
    --ack-deadline=60 \
    --message-retention-duration=1d \
    --expiration-period=never

# Gmail's service agent publishes to this topic on inbox changes.
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
    --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
    --role="roles/pubsub.publisher"
```

Record `projects/$PROJECT/topics/$TOPIC` and
`projects/$PROJECT/subscriptions/$SUB` — they go into `.env` next.

## 2. Local OAuth re-auth (one-time)

Existing token.json lacks the `pubsub` scope. Re-run the interactive
flow to include it.

```powershell
# Windows workstation
python agents/connector/scripts/gmail-auth.py
```

Browser opens, authorize the operator's account, token lands at
`~/.clawford/connector-workspace/token.json`. The script prints the
`scp` command to run next.

## 3. SCP token to VPS

```bash
scp ~/.clawford/connector-workspace/token.json \
    openclaw@<your-tailscale-host>:~/.clawford/connector-workspace/token.json
```

## 4. Update .env on VPS

SSH in, append the two Pub/Sub identifiers:

```bash
ssh openclaw@<your-tailscale-host>
sudo -u openclaw tee -a ~/clawford/.env <<'EOF'

# Huckle real-time Gmail triage (2026-04-20)
GMAIL_PUSH_TOPIC=projects/<PROJECT>/topics/gmail-huckle-inbox
GMAIL_PUSH_SUBSCRIPTION=projects/<PROJECT>/subscriptions/huckle-gmail-pull
EOF
```

## 5. Deploy code (on VPS)

```bash
cd ~/repo && git pull
python3 agents/shared/deploy.py connector --yes-updates
```

## 6. Install systemd unit + daemon-reload

```bash
sudo cp ops/systemd/clawford-huckle-push.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable clawford-huckle-push
```

Don't `--now` yet — seed the watch state first (next step) so the
listener has a cursor when it boots.

## 7. Seed the watch state

```bash
python3 ~/.clawford/connector-workspace/scripts/gmail-watch-renew.py
```

Expected stdout:
```json
{"status":"ok","history_id":"...","expiration_ms":...,"topic":"projects/.../topics/gmail-huckle-inbox"}
```

Verify state file:
```bash
cat ~/.clawford/connector-workspace/cache/gmail-watch-state.json
```

## 8. Start the listener

```bash
sudo systemctl start clawford-huckle-push
sudo systemctl status clawford-huckle-push
tail -f ~/.clawford/logs/huckle-push.log
```

Expected log line: `[listener] starting. subscription=... cursor=...`

## 9. Install the daily renewal cron

```bash
bash ~/repo/ops/scripts/install-host-cron.sh
```

Expected new line:
```
0 7 * * * /home/openclaw/repo/ops/scripts/script-contract-host.sh
  connector-gmail-watch-renew
  /home/openclaw/.clawford/connector-workspace/scripts/gmail-watch-renew.py
  CONNECTOR_BOT_TOKEN 120  # script-contract-connector-gmail-watch-renew
```

## 10. End-to-end smoke test

Send a test email from a second address to the operator's inbox with
subject "huckle push test — ignore".

Within 30-60s expect:
- `tail ~/.clawford/logs/huckle-push.log` shows `tick: {"pulled":1, ...}`
- `cat ~/.clawford/connector-workspace/cache/triage-queue.json` grows
- A draft appears in Gmail Drafts (if sender matches an existing
  people file)
- Telegram ping arrives from Huckle with the draft summary

Then delete the test draft + inbound.

## Ops notes

- **Disable temporarily:** `touch ~/.clawford/huckle-push-disabled` and
  `sudo systemctl stop clawford-huckle-push`. Re-enable by removing the
  file + `systemctl start`.
- **Renewal alerts:** the heartbeat probe (every 15 min) fires a
  Telegram alert if the watch expires within 36h OR the listener
  service isn't active. No alerts when healthy.
- **Belt-and-suspenders:** if push is broken, the existing `*/30`
  inbox-triage and `:05/:35` auto-compose crons still run — 30-min
  worst-case latency, same as before the push pipeline shipped.
