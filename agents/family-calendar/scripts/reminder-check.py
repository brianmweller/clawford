#!/usr/bin/env python3
"""
reminder-check.py — Poll for upcoming events and emit reminders.

Checks all configured Google Calendars for events in the next 60 minutes.
Deduplicates against sent-reminders.json to avoid re-sending.
Outputs JSON array of reminders to send (or empty array if none).

Usage: python3 reminder-check.py

Output JSON (to stdout):
  [
    {
      "event_id": "...",
      "summary": "Dentist",
      "calendar_label": "Sam",
      "calendar_emoji": "👨",
      "starts_in_min": 30,
      "location": "123 Main St",
      "tier": "30min"
    }
  ]

Reminder tiers:
  - 60min: events with location containing travel keywords
  - 30min: standard events (default)
  - 15min: pickup/dropoff events

Dedup key: {event_id}_{calendar_id}_{tier}
"""

import json
import os
import re
import sys
import urllib.request as urllib_request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.meeting_classifier import has_videoconference_link  # noqa: E402
from agents.shared.calendar_index import meeting_event_ids  # noqa: E402
from agents.shared import brain_tasks  # noqa: E402

# Surfacer lives at agents/family-calendar/task_surfacer.py — added to sys.path
# above. Import after the brain-tasks import so the surfacer's ``from
# brain_tasks import Task`` resolves cleanly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_surfacer  # type: ignore  # noqa: E402

WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")
REMINDERS_PATH = os.path.join(WORKSPACE, "sent-reminders.json")
CONFIG_PATH = os.path.join(WORKSPACE, "calendar-config.json")
TOKEN_PATH = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(WORKSPACE, "token.json"),
)
BRAIN_INDEX_PATH = os.environ.get(
    "CLAWFORD_CALENDAR_INDEX_PATH",
    os.path.expanduser("~/Dropbox/openclaw-backup/status/calendar-index.json"),
)


def _load_brain_meeting_ids() -> set:
    """Shared-brain authoritative set of ids Mouse must skip (Murphy's).
    Degrades to empty set so the local has_videoconference_link check
    owns when the index is missing/stale."""
    try:
        return meeting_event_ids(BRAIN_INDEX_PATH)
    except Exception:
        return set()

TRAVEL_KEYWORDS = re.compile(
    r"airport|doctor|dentist|hospital|clinic|urgent care|emergency",
    re.IGNORECASE,
)
PICKUP_KEYWORDS = re.compile(
    r"pickup|pick up|pick-up|drop off|drop-off|dropoff|school pickup|school drop",
    re.IGNORECASE,
)


def load_sent_reminders():
    if not os.path.exists(REMINDERS_PATH):
        return {"reminders": {}, "last_pruned": None}
    with open(REMINDERS_PATH) as f:
        return json.load(f)


def save_sent_reminders(data):
    with open(REMINDERS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def prune_old_reminders(data):
    """Remove entries older than 48 hours."""
    now = datetime.now(timezone.utc)
    last_pruned = data.get("last_pruned")

    if last_pruned:
        try:
            lp = datetime.fromisoformat(last_pruned)
            if (now - lp).total_seconds() < 86400:  # Less than 24h since last prune
                return data
        except (ValueError, TypeError):
            pass

    cutoff = (now - timedelta(hours=48)).isoformat()
    reminders = data.get("reminders", {})
    data["reminders"] = {k: v for k, v in reminders.items() if v > cutoff}
    data["last_pruned"] = now.isoformat()
    return data


def classify_tier(summary, location):
    """Determine reminder tier based on event content."""
    text = f"{summary} {location}"
    if TRAVEL_KEYWORDS.search(text):
        return "60min", 60
    if PICKUP_KEYWORDS.search(summary):
        return "15min", 15
    return "30min", 30


def format_reminder_message(reminder: dict) -> str:
    """Render one reminder as the Telegram body text.

    Output shape:
      "🐭 Heads up — {emoji} {label} {summary} in {minutes} min ({location})"
    Emoji and location are both optional — each is omitted gracefully
    when empty.
    """
    emoji = reminder.get("calendar_emoji", "")
    label = reminder.get("calendar_label", "")
    summary = reminder.get("summary", "(No title)")
    minutes = reminder.get("starts_in_min", 0)
    location = reminder.get("location", "")

    who = f"{emoji} {label}".strip()
    base = f"🐭 Heads up — {who} {summary} in {minutes} min"
    if location:
        base += f" ({location})"
    return base


def send_telegram(bot_token: str, chat_id: str, text: str, reply_markup: dict | None = None) -> bool:
    """POST one reminder to the Telegram Bot API.

    ``reply_markup`` is an optional inline-keyboard dict (see
    ``build_task_keyboard``). When provided, it's passed through so task
    reminders get actionable done/snooze/ignore buttons. Event reminders
    omit it and get a plain text message.
    """
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    payload = json.dumps(body).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib_request.urlopen(req, timeout=15)
        body = json.loads(resp.read())
        return bool(body.get("ok"))
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)
        return False


def format_task_reminder_message(payload: dict) -> str:
    """Render a task ping as Telegram body text.

    T-30min:   "🐭📋 Heads up — {description} in 30 min"
    T+24h:     "🐭📋 Overdue — {description} (was due {pretty_due})"
    """
    desc = payload.get("description", "(untitled task)")
    tier = payload.get("tier")
    if tier == "t_30min":
        return f"🐭📋 Heads up — {desc} in 30 min"
    # t_24h_overdue — include the original due for context
    due_at = payload.get("due_at", "")
    # Strip time zone marker and seconds for display; best-effort.
    pretty = due_at.replace("T", " ").replace("Z", " UTC")
    return f"🐭📋 Overdue — {desc} (was due {pretty})"


def build_task_keyboard(task_id: str) -> dict:
    """Inline keyboard with three callback buttons: done, snooze, ignore.

    Prefix convention matches ``agents/shared/dispatcher.py`` — see the
    ``_TASK_CALLBACK_PREFIXES`` group that routes these to
    ``handle_task_callback``.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ done", "callback_data": f"task_done:{task_id}"},
                {"text": "⏭ snooze", "callback_data": f"task_snooze:{task_id}"},
                {"text": "🚫 ignore", "callback_data": f"task_ignore:{task_id}"},
            ],
        ]
    }


def get_credentials():
    """Load OAuth2 credentials."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None, "google-auth not installed"

    if not os.path.exists(TOKEN_PATH):
        return None, "token.json not found"

    with open(TOKEN_PATH) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_data["token"] = creds.token
            with open(TOKEN_PATH, "w") as f:
                json.dump(token_data, f, indent=2)
        except Exception as e:
            return None, f"Token refresh failed: {e}"

    return creds, None


def main():
    # Load config
    if not os.path.exists(CONFIG_PATH):
        print(json.dumps({"status": "ok", "sent": 0}))
        return

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    # Get credentials
    creds, err = get_credentials()
    if err:
        print(json.dumps({
            "status": "error",
            "sent": 0,
            "alert": f"🐭 reminder-check auth failed: {err}",
        }))
        return

    # Build service
    try:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds)
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "sent": 0,
            "alert": f"🐭 reminder-check gcal build failed: {e}",
        }))
        return

    # Time window: now to now+60min
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(minutes=60)).isoformat()

    # Load sent reminders
    sent_data = load_sent_reminders()
    sent_data = prune_old_reminders(sent_data)
    sent_reminders = sent_data.get("reminders", {})

    # Mouse/Murphy routing boundary (memory:
    # project_meeting_event_routing.md): Murphy owns events with a
    # videoconference link. Authoritative set comes from the shared
    # brain calendar index (populated at 10:25 UTC by
    # calendar-index-build.py); local has_videoconference_link is the
    # fallback for events the index hasn't seen yet.
    brain_meeting_ids = _load_brain_meeting_ids()
    reminders_to_send = []

    for cal in config.get("calendars", []):
        if not cal.get("remind", True):
            continue

        cal_id = cal["id"]
        label = cal["label"]
        emoji = cal.get("emoji", "")

        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            ).execute()

            for event in result.get("items", []):
                if event.get("status") == "cancelled":
                    continue

                start = event.get("start", {})
                if "date" in start:  # Skip all-day events
                    continue

                event_id = event.get("id", "")

                # Routing boundary: Murphy owns videoconferenced events.
                # Brain index first (authoritative for description-only
                # links), local classifier as fallback.
                if event_id in brain_meeting_ids:
                    continue
                if has_videoconference_link(event):
                    continue

                summary = event.get("summary", "(No title)")
                location = event.get("location", "")
                start_time = start.get("dateTime", "")

                if not start_time:
                    continue

                # Calculate minutes until event
                try:
                    event_start = datetime.fromisoformat(start_time)
                    minutes_until = (event_start - now).total_seconds() / 60
                except (ValueError, TypeError):
                    continue

                if minutes_until < 0:
                    continue

                # Determine tier
                tier, tier_minutes = classify_tier(summary, location)

                # Check if this reminder should fire
                if minutes_until > tier_minutes:
                    continue

                # Check dedup
                dedup_key = f"{event_id}_{cal_id}_{tier}"
                if dedup_key in sent_reminders:
                    continue

                reminders_to_send.append({
                    "event_id": event_id,
                    "summary": summary,
                    "calendar_label": label,
                    "calendar_emoji": emoji,
                    "starts_in_min": round(minutes_until),
                    "location": location,
                    "tier": tier,
                    "_dedup_key": dedup_key,
                })

        except Exception as e:
            print(f"Error fetching {label}: {e}", file=sys.stderr)

    # Send each reminder via Telegram, and only mark the dedup key in
    # sent_reminders AFTER the POST returns ok. This fixes a pre-existing
    # bug where the old LLM cron marked reminders sent BEFORE actually
    # calling the Telegram tool — a failed send left the user without
    # a reminder but with a "sent" flag that blocked retry.
    bot_token = os.environ.get("FAMILYCAL_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    sent_count = 0
    failed_count = 0
    for reminder in reminders_to_send:
        dedup_key = reminder.pop("_dedup_key")
        msg = format_reminder_message(reminder)
        if send_telegram(bot_token, chat_id, msg):
            sent_reminders[dedup_key] = now.isoformat()
            sent_count += 1
        else:
            failed_count += 1

    # Task pings — share sent-reminders.json dedup cache with events.
    try:
        tasks = brain_tasks.read_tasks()
    except Exception as e:
        print(f"brain_tasks.read_tasks failed: {e}", file=sys.stderr)
        tasks = []
    task_payloads = task_surfacer.pending_reminders(
        tasks, now, already_sent=set(sent_reminders.keys())
    )
    for payload in task_payloads:
        msg = format_task_reminder_message(payload)
        kb = build_task_keyboard(payload["task_id"])
        if send_telegram(bot_token, chat_id, msg, reply_markup=kb):
            sent_reminders[payload["dedup_key"]] = now.isoformat()
            sent_count += 1
        else:
            failed_count += 1

    # Save updated sent reminders
    sent_data["reminders"] = sent_reminders
    save_sent_reminders(sent_data)

    # SCRIPT_CONTRACT-compliant stdout line.
    result: dict = {"status": "ok", "sent": sent_count}
    if failed_count:
        result["status"] = "degraded"
        result["failed"] = failed_count
        result["alert"] = (
            f"🐭 reminder-check: {failed_count} telegram send(s) failed "
            f"(FAMILYCAL_BOT_TOKEN or TELEGRAM_CHAT_ID missing?)"
        )
    print(json.dumps(result))


if __name__ == "__main__":
    import json as _contract_json
    import sys as _contract_sys
    _contract_status = "ok"
    _contract_error = None
    try:
        _contract_rc = main()
        if _contract_rc not in (0, None):
            _contract_status = "error"
            _contract_error = f"main returned {_contract_rc}"
    except SystemExit as _contract_e:
        if _contract_e.code not in (0, None):
            _contract_status = "error"
            _contract_error = f"main exited with code {_contract_e.code}"
    except BaseException as _contract_e:  # noqa: BLE001
        _contract_status = "error"
        _contract_error = str(_contract_e)[:200]
    _contract_envelope = {"status": _contract_status}
    if _contract_error:
        _contract_envelope["error"] = _contract_error
    print(_contract_json.dumps(_contract_envelope))
    _contract_sys.exit(0)
