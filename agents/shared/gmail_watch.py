"""gmail_watch — thin wrappers around Gmail users.watch() + users.stop().

Used by the connector's real-time triage pipeline (gmail-push-listener +
gmail-watch-renew). Pure helpers so tests pass fake service objects
without hitting the network.

Gmail `users.watch` subscribes the authenticated mailbox to Pub/Sub
notifications on a given topic. The returned `historyId` is the cursor
the listener uses as the baseline for its first `users.history.list()`
delta query. `expiration` is unix-epoch-ms; Gmail invalidates the watch
after ~7 days, so a daily renewal cron is the standard pattern.

Required OAuth scope: at minimum `gmail.readonly` (`watch()` requires
read-access; `compose` alone is insufficient). The connector already
holds `readonly`.

WatchState is the persisted shape the listener reads on startup. It is
an append-ONCE local cursor — the listener mutates a separate
`gmail-history-cursor.json` on every tick. Watch state itself only
changes when the renewal cron runs.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_LABEL_IDS: tuple[str, ...] = ("INBOX",)


def create_watch(
    service: Any,
    *,
    topic_name: str,
    label_ids: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Call Gmail users.watch() and return the raw API response.

    topic_name is the fully-qualified Pub/Sub topic, e.g.
    "projects/my-proj/topics/gmail-huckle-inbox". The caller is
    responsible for having granted the Gmail service agent
    (`gmail-api-push@system.gserviceaccount.com`) the `pubsub.publisher`
    role on that topic — that IAM binding is a prerequisite the API
    itself doesn't verify on watch().

    The response has shape {historyId, expiration}. expiration is a
    unix-epoch-ms string; Gmail treats watches as expiring after 7
    days regardless of expiration, so do not trust a value >7d.
    """
    labels = list(label_ids) if label_ids is not None else list(DEFAULT_LABEL_IDS)
    body = {
        "topicName": topic_name,
        "labelIds": labels,
        "labelFilterAction": "include",
    }
    return service.users().watch(userId="me", body=body).execute()


def stop_watch(service: Any) -> None:
    """Unsubscribe the mailbox from Pub/Sub notifications.
    Idempotent: Gmail returns 204 whether or not a watch was active."""
    service.users().stop(userId="me").execute()


@dataclass
class WatchState:
    """Persisted summary of the last successful users.watch() call.
    Consumed by gmail-push-listener on startup to seed its history
    cursor, and by the heartbeat probe to alert on near-expiration.
    """
    history_id: str
    expiration_ms: int
    topic: str

    def is_expiring_within(self, *, hours: float, now_ms: int | None = None) -> bool:
        """True if the watch expires within the given number of hours
        from `now_ms` (default: wall clock). Treats any value in the
        past as expired."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return self.expiration_ms <= now + int(hours * 3600_000)


def save_watch_state(path: Path | str, state: WatchState) -> None:
    """Write a WatchState to disk atomically. Creates parent dirs as
    needed so the caller can point at a cache path that doesn't exist
    yet."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    tmp.replace(p)


def load_watch_state(path: Path | str) -> WatchState | None:
    """Read a WatchState from disk. Returns None on any failure
    (missing file, corrupt JSON, missing fields) so the caller can
    treat "no state" and "bad state" identically."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return WatchState(
            history_id=str(data["history_id"]),
            expiration_ms=int(data["expiration_ms"]),
            topic=str(data["topic"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
