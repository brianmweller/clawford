#!/usr/bin/env python3
"""gcal-tasks-sync.py — two-way sync between ``tasks/queue.md`` and
Google Tasks.

Runs every 5 min from host cron (``*/5 * * * *``) next to
``family-calendar-reminder-check``. queue.md is canonical; Google Tasks
is the mobile surface where the operator checks things off from his phone.

Push pass (local → remote):
    - Unmapped local tasks with ``assignee=me`` and ``status=open`` get
      CREATEd on the Sam.M.Smith list.
    - Mapped tasks with local diffs (description, due_at, status) get
      PATCHed.
    - Status ``ignored`` prefixes the remote title with ``[IGNORED] ``
      and sets remote status to ``completed``.
    - Status ``deleted`` DELETEs the remote task and tombstones the
      state-map entry.

Pull pass (remote → local):
    - Remote ``completed`` with local ``open`` → flip local to ``done``.
    - Remote ``deleted`` with mapped local → append ``status=deleted``
      tombstone to queue.md.
    - Remote ``needsAction`` with local ``ignored`` → unignore local
      back to ``open`` (next push will strip the prefix).
    - Unmapped remote tasks (created on the phone) are logged and
      skipped. v1 does not auto-import.

Conflict resolution — local canonical for description, timestamp-based
last-write-wins for status via the state map. See plan-file and the
Design doc for the full conflict matrix.

State file:
    ~/.clawford/family-calendar-workspace/tasks-sync-state.json
Token / credentials:
    ~/.clawford/family-calendar-workspace/token.json
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request as urllib_request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

# --- shared library sys.path shim ---
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared import brain_tasks  # noqa: E402


WORKSPACE = os.path.expanduser("~/.clawford/family-calendar-workspace")
STATE_PATH = os.path.join(WORKSPACE, "tasks-sync-state.json")
TOKEN_PATH = os.environ.get(
    "GOOGLE_CALENDAR_TOKEN_PATH",
    os.path.join(WORKSPACE, "token.json"),
)
TARGET_LIST_TITLE = os.environ.get("GTASKS_LIST_TITLE", "Sam.M.Smith's list")

IGNORED_PREFIX = "[IGNORED] "
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def new_state(list_id: str | None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "list_id": list_id,
        "last_pull_at": None,
        "last_pull_updated_min": None,
        "last_push_at": None,
        "map": {},
    }


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return new_state(None)
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return new_state(None)


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# List lookup
# ---------------------------------------------------------------------------


def find_list_id(service, target_title: str) -> str | None:
    """Return the list id whose title matches ``target_title`` exactly,
    or ``None`` if no such list exists."""
    resp = service.tasklists().list().execute()
    for item in resp.get("items", []):
        if item.get("title") == target_title:
            return item.get("id")
    return None


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def hash_description(s: str) -> str:
    return "sha1:" + hashlib.sha1(s.encode("utf-8")).hexdigest()


def _due_iso(task: brain_tasks.Task) -> str | None:
    """Convert a task's ``due_at`` to RFC 3339 for the Google Tasks API.
    All-day tasks (date-only) are promoted to midnight UTC."""
    if task.due_at is None:
        return None
    if task.is_timed():
        return task.due_at
    return task.due_at + "T00:00:00.000Z"


def _title_for_remote(task: brain_tasks.Task) -> str:
    desc = task.description or "(untitled)"
    if task.status == "ignored":
        return IGNORED_PREFIX + desc
    return desc


def build_create_payload(task: brain_tasks.Task) -> dict:
    payload: dict = {
        "title": _title_for_remote(task),
        "status": "completed" if task.status in {"done", "ignored", "cancelled"} else "needsAction",
    }
    due = _due_iso(task)
    if due is not None:
        payload["due"] = due
    return payload


def build_patch_payload(task: brain_tasks.Task, entry: dict) -> dict | None:
    """Return a PATCH body capturing the diff vs. the state-map entry, or
    ``None`` if nothing changed (skip the API call)."""
    patch: dict = {}

    # Description
    new_hash = hash_description(task.description or "")
    if new_hash != entry.get("last_pushed_description_hash") or task.status == "ignored":
        # Title carries the [IGNORED] prefix when status is ignored, so a
        # status transition to ignored re-renders the title even if the
        # raw description is unchanged.
        new_title = _title_for_remote(task)
        patch["title"] = new_title

    # Due
    new_due = _due_iso(task)
    if new_due != entry.get("last_pushed_due_at"):
        if new_due is not None:
            patch["due"] = new_due
        else:
            # Google Tasks drops `due` by omission — send empty dict key
            # semantics via explicit payload. Skip for v1; require due_at
            # changes to always set a new value.
            pass

    # Status
    new_remote_status = "completed" if task.status in {"done", "ignored", "cancelled"} else "needsAction"
    old_remote_status = "completed" if entry.get("last_pushed_status") in {"done", "ignored", "cancelled"} else "needsAction"
    if new_remote_status != old_remote_status:
        patch["status"] = new_remote_status

    return patch if patch else None


# ---------------------------------------------------------------------------
# Push pass
# ---------------------------------------------------------------------------


def push_pass(service, state: dict, tasks: list[brain_tasks.Task]) -> None:
    """Create / patch / delete remote tasks to reflect local state. The
    caller supplies ``tasks`` — typically ``brain_tasks.read_tasks()`` or
    ``read_tasks(include_deleted=True)`` when tombstones need pushing."""
    list_id = state.get("list_id")
    if not list_id:
        return

    for t in tasks:
        if t.assignee != "me":
            continue
        entry = state["map"].get(t.id)

        if entry is None:
            if t.status != "open":
                # Terminal-from-birth task — nothing to push to remote.
                continue
            body = build_create_payload(t)
            created = service.tasks().insert(tasklist=list_id, body=body).execute()
            state["map"][t.id] = {
                "gcal_task_id": created.get("id"),
                "gcal_updated": created.get("updated"),
                "last_pushed_status": t.status,
                "last_pushed_due_at": _due_iso(t),
                "last_pushed_description_hash": hash_description(t.description or ""),
                "last_synced_at": _iso_now(),
                "tombstoned": False,
            }
            continue

        # Already mapped — handle delete, then patch.
        if t.status == "deleted" and not entry.get("tombstoned"):
            service.tasks().delete(tasklist=list_id, task=entry["gcal_task_id"]).execute()
            entry["tombstoned"] = True
            entry["last_pushed_status"] = "deleted"
            entry["last_synced_at"] = _iso_now()
            continue

        if entry.get("tombstoned"):
            continue

        patch = build_patch_payload(t, entry)
        if patch is None:
            continue
        resp = service.tasks().patch(
            tasklist=list_id, task=entry["gcal_task_id"], body=patch
        ).execute()
        entry["gcal_updated"] = resp.get("updated")
        entry["last_pushed_status"] = t.status
        entry["last_pushed_due_at"] = _due_iso(t)
        entry["last_pushed_description_hash"] = hash_description(t.description or "")
        entry["last_synced_at"] = _iso_now()

    state["last_push_at"] = _iso_now()


# ---------------------------------------------------------------------------
# Pull pass
# ---------------------------------------------------------------------------


def pull_pass(service, state: dict) -> None:
    list_id = state.get("list_id")
    if not list_id:
        return

    kwargs = {"tasklist": list_id, "showCompleted": True, "showHidden": True, "showDeleted": True}
    if state.get("last_pull_updated_min"):
        kwargs["updatedMin"] = state["last_pull_updated_min"]
    resp = service.tasks().list(**kwargs).execute()

    reverse = {v["gcal_task_id"]: k for k, v in state["map"].items() if v.get("gcal_task_id")}
    local_tasks = {t.id: t for t in brain_tasks.read_tasks(include_deleted=True)}

    for gt in resp.get("items", []):
        local_id = reverse.get(gt.get("id"))
        if local_id is None:
            print(
                f"tasks-sync: unmapped remote task {gt.get('id')} title={gt.get('title')!r}",
                file=sys.stderr,
            )
            continue
        entry = state["map"][local_id]
        local = local_tasks.get(local_id)
        if local is None:
            # Mapped but the local task disappeared — data corruption.
            continue

        remote_status = gt.get("status", "needsAction")
        remote_deleted = bool(gt.get("deleted"))

        # Remote deleted → propagate as tombstone
        if remote_deleted and local.status != "deleted":
            brain_tasks.edit_task_status(local_id, "deleted")
            entry["last_pushed_status"] = "deleted"
            entry["tombstoned"] = True
            entry["last_synced_at"] = _iso_now()
            continue

        # Remote completed, local still open → pull wins
        if remote_status == "completed" and local.status == "open":
            brain_tasks.edit_task_status(
                local_id, "done", completed_at=gt.get("completed") or _iso_now()
            )
            entry["last_pushed_status"] = "done"
            entry["last_synced_at"] = _iso_now()
            continue

        # Remote back to needsAction from locally-ignored → unignore
        if remote_status == "needsAction" and local.status == "ignored":
            brain_tasks.edit_task_status(local_id, "open")
            entry["last_pushed_status"] = "open"
            entry["last_synced_at"] = _iso_now()
            continue

        # Otherwise no-op; update map bookkeeping
        entry["gcal_updated"] = gt.get("updated")

    state["last_pull_at"] = _iso_now()
    state["last_pull_updated_min"] = _iso_now()


# ---------------------------------------------------------------------------
# Credentials + service
# ---------------------------------------------------------------------------


def get_credentials():
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


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib_request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urllib_request.urlopen(req, timeout=15)
        body = json.loads(resp.read())
        return bool(body.get("ok"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    creds, err = get_credentials()
    if err:
        print(json.dumps({
            "status": "error", "sent": 0,
            "alert": f"🐭📋 tasks-sync auth failed: {err}",
        }))
        return

    try:
        from googleapiclient.discovery import build
        service = build("tasks", "v1", credentials=creds)
    except Exception as e:
        print(json.dumps({
            "status": "error", "sent": 0,
            "alert": f"🐭📋 tasks-sync service build failed: {e}",
        }))
        return

    state = load_state()

    # Resolve list id on first run (or if it's been cleared).
    if not state.get("list_id"):
        lid = find_list_id(service, TARGET_LIST_TITLE)
        if lid is None:
            bot_token = os.environ.get("FAMILYCAL_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
            send_telegram(
                bot_token, chat_id,
                f"🐭📋 tasks-sync: list {TARGET_LIST_TITLE!r} not found on your Google account — "
                "verify the list exists or set GTASKS_LIST_TITLE.",
            )
            print(json.dumps({
                "status": "error", "sent": 0,
                "alert": f"🐭📋 tasks list {TARGET_LIST_TITLE!r} not found",
            }))
            return
        state["list_id"] = lid

    try:
        local_tasks = brain_tasks.read_tasks(include_deleted=True)
    except Exception as e:
        print(json.dumps({
            "status": "error", "sent": 0,
            "alert": f"🐭📋 tasks-sync brain read failed: {e}",
        }))
        return

    try:
        push_pass(service, state, local_tasks)
    except Exception as e:
        print(f"push_pass error: {e}", file=sys.stderr)
    try:
        pull_pass(service, state)
    except Exception as e:
        print(f"pull_pass error: {e}", file=sys.stderr)

    save_state(state)
    print(json.dumps({"status": "ok"}))


if __name__ == "__main__":
    main()
