"""agents/shared/telegram_inbox.py — async long-polling Telegram daemon.

One long-running process. Six concurrent poll tasks (one per bot).
Every inbound update is routed to `dispatcher.dispatch(agent_id, update)`
which does all the real work (chat_id gate, LLM call, reply).

Design:
- Uses httpx.AsyncClient if available, falls back to urllib sync calls
  (wrapped in asyncio.to_thread) if httpx isn't installed.
- Persists per-agent offsets at ~/.clawford/inbox/<agent>.offset so a
  crash + restart doesn't double-deliver recent messages.
- Kill-switch file: if ~/.clawford/inbox-disabled exists, main() exits
  cleanly. Local dev toggle to stop the VPS daemon without systemctl.
- Backoff-on-error: any RuntimeError in a poll_loop → log + sleep 5s +
  retry. The loop never dies; individual failures are swallowed.
- Dispatch errors are swallowed PER UPDATE — an update that crashes
  dispatch still advances the offset so we don't infinite-loop on it.

Run: python3 -m agents.shared.telegram_inbox  (on VPS under systemd)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Awaitable, Callable

# Make sibling modules importable when run as a script.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

import dispatcher  # type: ignore
import conversation  # type: ignore

log = logging.getLogger("telegram_inbox")

LONG_POLL_TIMEOUT_S = 25
BACKOFF_S = 5

KILL_SWITCH_PATH = os.path.expanduser("~/.clawford/inbox-disabled")

AGENT_TOKEN_ENV = {
    # Order matters: first env var that's set wins. Mirrors the same
    # list in dispatcher.py.
    "fix-it": ["FIXIT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"],
    "news-digest": ["NEWSDIGEST_BOT_TOKEN"],
    "shopping": ["SHOPPING_BOT_TOKEN"],
    "family-calendar": ["FAMILYCAL_BOT_TOKEN"],
    "meetings-coach": ["MEETINGS_BOT_TOKEN"],
    "connector": ["CONNECTOR_BOT_TOKEN"],
}


# ---------------------------------------------------------------------------
# Offset persistence
# ---------------------------------------------------------------------------


def _offset_path(agent_id: str) -> Path:
    inbox_dir = os.environ.get("CLAWFORD_INBOX_DIR") or os.path.expanduser("~/.clawford/inbox")
    return Path(inbox_dir) / f"{agent_id}.offset"


def load_offset(agent_id: str) -> int:
    p = _offset_path(agent_id)
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return 0


def save_offset(agent_id: str, offset: int) -> None:
    p = _offset_path(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".offset.tmp")
    tmp.write_text(str(offset))
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def is_disabled() -> bool:
    return os.path.exists(KILL_SWITCH_PATH)


# ---------------------------------------------------------------------------
# Bot config
# ---------------------------------------------------------------------------


def load_bot_configs() -> list[tuple[str, str]]:
    """Return [(agent_id, token), ...] for every agent with a bot token
    set in env. Agents without a token are silently skipped. For
    agents with multiple candidate env vars, the first one that's set
    wins."""
    configs = []
    for agent_id, env_candidates in AGENT_TOKEN_ENV.items():
        for name in env_candidates:
            token = os.environ.get(name, "").strip()
            if token:
                configs.append((agent_id, token))
                break
    return configs


# ---------------------------------------------------------------------------
# Network layer — long-poll getUpdates
# ---------------------------------------------------------------------------


async def get_updates_httpx(token: str, offset: int, timeout: int) -> dict:
    """getUpdates via httpx.AsyncClient (preferred)."""
    import httpx  # local import so tests that patch it still work

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"offset": offset, "timeout": timeout}
    async with httpx.AsyncClient(timeout=timeout + 5) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def get_updates_urllib(token: str, offset: int, timeout: int) -> dict:
    """Fallback: urllib in a thread. Slower but has no deps."""
    import urllib.request
    import urllib.parse

    def _do():
        url = (
            f"https://api.telegram.org/bot{token}/getUpdates"
            f"?offset={offset}&timeout={timeout}"
        )
        with urllib.request.urlopen(url, timeout=timeout + 5) as r:
            return json.loads(r.read())
    return await asyncio.to_thread(_do)


def _pick_get_updates() -> Callable[[str, int, int], Awaitable[dict]]:
    try:
        import httpx  # noqa: F401
        return get_updates_httpx
    except ImportError:
        return get_updates_urllib


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


async def poll_loop(
    agent_id: str,
    token: str,
    get_updates: Callable[[str, int, int], Awaitable[dict]],
    dispatch: Callable[[str, dict], None],
    stop: asyncio.Event,
) -> None:
    """Long-poll getUpdates for one bot until `stop` is set.

    get_updates(token, offset, timeout) → dict from Telegram API (with
    `ok` and `result` fields). Injection point for tests.
    dispatch(agent_id, update) is called synchronously for each update
    (via asyncio.to_thread in production since the dispatcher is sync).
    """
    log.info("poll_loop started for %s", agent_id)
    offset = load_offset(agent_id)

    while not stop.is_set():
        # Guaranteed yield point so cooperative scheduling works even
        # when get_updates or dispatch don't themselves yield (tests
        # with synchronous fakes, mostly).
        await asyncio.sleep(0)
        if stop.is_set():
            break
        try:
            response = await get_updates(token, offset, LONG_POLL_TIMEOUT_S)
        except Exception:
            log.warning("getUpdates failed for %s:\n%s", agent_id, traceback.format_exc())
            try:
                await asyncio.wait_for(stop.wait(), timeout=BACKOFF_S)
            except asyncio.TimeoutError:
                pass
            continue

        if not response.get("ok"):
            log.warning("getUpdates not-ok for %s: %s", agent_id, response)
            try:
                await asyncio.wait_for(stop.wait(), timeout=BACKOFF_S)
            except asyncio.TimeoutError:
                pass
            continue

        updates = response.get("result") or []
        for update in updates:
            update_id = update.get("update_id", 0)
            try:
                # dispatch is sync — run off the loop so the poll loop
                # stays responsive. For tests we might pass a direct
                # callable so just call it if it's not a coroutine.
                if asyncio.iscoroutinefunction(dispatch):
                    await dispatch(agent_id, update)
                else:
                    result = dispatch(agent_id, update)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                log.error("dispatch crashed for %s update %s:\n%s",
                          agent_id, update_id, traceback.format_exc())
            # Always advance past this update, even if dispatch crashed.
            offset = max(offset, update_id + 1)

        if updates:
            try:
                save_offset(agent_id, offset)
            except OSError as exc:
                log.warning("offset save failed for %s: %s", agent_id, exc)

        # tight loop when updates are present; when empty, long-poll
        # already waited so we go right back into getUpdates

    log.info("poll_loop stopped for %s", agent_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _async_main() -> int:
    if is_disabled():
        log.info("inbox-disabled file present — exiting cleanly")
        return 0

    configs = load_bot_configs()
    if not configs:
        log.error("no bot tokens configured — nothing to poll")
        return 2

    log.info("starting inbox with %d bots: %s", len(configs), [c[0] for c in configs])

    get_updates = _pick_get_updates()
    stop = asyncio.Event()

    def _sync_dispatch(agent_id: str, update: dict) -> None:
        dispatcher.dispatch(agent_id, update)

    async def _threaded_dispatch(agent_id: str, update: dict) -> None:
        await asyncio.to_thread(_sync_dispatch, agent_id, update)

    tasks = [
        asyncio.create_task(
            poll_loop(agent_id, token, get_updates, _threaded_dispatch, stop)
        )
        for agent_id, token in configs
    ]

    # Kill-switch watcher — if the file appears at runtime, stop gracefully.
    async def _kill_switch_watcher():
        while not stop.is_set():
            if is_disabled():
                log.info("kill-switch file appeared — stopping")
                stop.set()
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    watcher = asyncio.create_task(_kill_switch_watcher())

    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(_async_main())
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
