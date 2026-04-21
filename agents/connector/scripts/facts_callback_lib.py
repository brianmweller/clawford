"""Facts-callback dispatcher — single entry point for Telegram
callbacks on brain-maintenance items.

Routes `facts:<action>:<arg>` (from agents/shared/dispatcher.py) to the
right operation across agents/shared/pending_queue.py and
agents/shared/pending_review_resolve.py. Keeps the shared dispatcher
thin: it parses the prefix, the connector-specific handler does the
real work.

Actions
-------
- approve          — promote the pending fact and remove the queue entry
- reject           — write to _rejected.md + remove the queue entry
- skip             — mute the queue entry for 7 days
- approve_remaining — bulk approve every currently-active queue entry
- silence_today    — bulk mute every active queue entry until tomorrow
- expand           — no-op here; the dispatcher handles re-rendering

Each return shape is {"status": "ok"|"not_found"|"error", "action": <str>, ...}.
Callers render a short confirmation message to Telegram from this.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared import pending_queue
from agents.shared import pending_review_resolve


SKIP_MUTE_DAYS = 7


def _parse_iso(ts: str) -> datetime:
    """Accept both 'Z' and offset ISO forms."""
    if ts.endswith("Z"):
        return datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(ts)


def _mute_until_days(now_iso: str, days: int) -> str:
    dt = _parse_iso(now_iso) + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mute_until_tomorrow_morning(now_iso: str) -> str:
    """Next day at 08:00 UTC — close enough to 'tomorrow morning' for
    the morning-brief reader to skip."""
    dt = _parse_iso(now_iso)
    tomorrow = (dt + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return tomorrow.strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Single-item actions ─────────────────────────────────────────────


def _approve_one(fact_id: str, *, facts_dir: Path, queue_path: Path, now_iso: str) -> dict:
    result = pending_review_resolve.approve(facts_dir, fact_id, recorded_at=now_iso)
    if result["status"] == "approved":
        pending_queue.remove(queue_path, fact_id)
        return {"status": "ok", "action": "approve", "fact_id": fact_id}
    return {"status": result["status"], "action": "approve", "fact_id": fact_id}


def _reject_one(fact_id: str, *, facts_dir: Path, queue_path: Path, now_iso: str) -> dict:
    result = pending_review_resolve.reject(facts_dir, fact_id, rejected_at=now_iso)
    if result["status"] == "rejected":
        pending_queue.remove(queue_path, fact_id)
        return {"status": "ok", "action": "reject", "fact_id": fact_id}
    return {"status": result["status"], "action": "reject", "fact_id": fact_id}


def _skip_one(fact_id: str, *, queue_path: Path, now_iso: str) -> dict:
    until = _mute_until_days(now_iso, SKIP_MUTE_DAYS)
    if pending_queue.mute(queue_path, fact_id, until):
        return {"status": "ok", "action": "skip", "fact_id": fact_id, "muted_until": until}
    return {"status": "not_found", "action": "skip", "fact_id": fact_id}


# ─── Bulk actions ────────────────────────────────────────────────────


def _approve_remaining(batch_id: str, *, facts_dir: Path, queue_path: Path, now_iso: str) -> dict:
    entries = pending_queue.load_active(queue_path, now=now_iso)
    approved = 0
    for e in entries:
        fid = str(e.get("id") or "")
        if not fid:
            continue
        r = pending_review_resolve.approve(facts_dir, fid, recorded_at=now_iso)
        if r["status"] == "approved":
            pending_queue.remove(queue_path, fid)
            approved += 1
    return {
        "status": "ok", "action": "approve_remaining",
        "batch_id": batch_id, "approved_count": approved,
    }


def _silence_today(batch_id: str, *, queue_path: Path, now_iso: str) -> dict:
    entries = pending_queue.load_active(queue_path, now=now_iso)
    until = _mute_until_tomorrow_morning(now_iso)
    silenced = 0
    for e in entries:
        fid = str(e.get("id") or "")
        if not fid:
            continue
        if pending_queue.mute(queue_path, fid, until):
            silenced += 1
    return {
        "status": "ok", "action": "silence_today",
        "batch_id": batch_id, "silenced_count": silenced, "muted_until": until,
    }


# ─── Public entry point ──────────────────────────────────────────────


def handle_facts_callback(
    action: str,
    arg: str,
    *,
    facts_dir: Path,
    queue_path: Path,
    now_iso: str,
) -> dict:
    """Route a `facts:<action>:<arg>` callback to its handler.

    Never raises on caller error — returns a structured dict so the
    dispatcher can render a confirmation or failure message.
    """
    try:
        if action == "approve":
            return _approve_one(arg, facts_dir=facts_dir, queue_path=queue_path, now_iso=now_iso)
        if action == "reject":
            return _reject_one(arg, facts_dir=facts_dir, queue_path=queue_path, now_iso=now_iso)
        if action == "skip":
            return _skip_one(arg, queue_path=queue_path, now_iso=now_iso)
        if action == "approve_remaining":
            return _approve_remaining(arg, facts_dir=facts_dir, queue_path=queue_path, now_iso=now_iso)
        if action == "silence_today":
            return _silence_today(arg, queue_path=queue_path, now_iso=now_iso)
        if action == "expand":
            # The dispatcher handles re-rendering; the lib call is a
            # structured ack so the shared callback-shortcut path can
            # still return True without special-casing.
            return {"status": "ok", "action": "expand", "batch_id": arg}
        return {
            "status": "error", "action": action,
            "detail": f"unknown action: {action!r}",
        }
    except Exception as exc:  # noqa: BLE001 — surface all failures as structured
        return {"status": "error", "action": action, "detail": str(exc)}
