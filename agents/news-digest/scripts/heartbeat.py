#!/usr/bin/env python3
"""heartbeat.py — News Digest (Lowly Worm) heartbeat probe.

Subclass of agents.shared.heartbeat_base.HeartbeatProbe. Verifies
required state files (preferences/model.json), checks that the
persistent LinkedIn Chromium profile directory exists, and verifies
today's `ranked-<date>.json` is present after 11:00 UTC (the
morning-edition cron is scheduled at 10:30 UTC).

The morning_edition probe is gated by UTC time-of-day: before 11:00
UTC, missing ranked-today is "pending" (fine); after 11:00, it's
"missing" (degraded). This lets heartbeat run at any time without
producing false alarms during the normal compose window.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- shared library sys.path shim ---
# Find the first ancestor containing agents/shared/ and prepend it to
# sys.path so `from agents.shared import X` resolves in both the local
# repo layout (where agents/shared/ lives at the repo root) and the
# deployed <workspace>/agents/shared/ layout that deploy.py's
# sync_shared_library creates inside the gateway container.
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.heartbeat_base import HeartbeatProbe


# morning-edition is scheduled at 30 10 * * * (10:30 UTC). Give it
# 30 minutes of slack for compose time; after 11:00 UTC the ranked
# file should exist or something is broken.
MORNING_EDITION_DEADLINE_UTC_HOUR = 11

CACHE_FILES = [
    ("last-morning-edition.json", "morning-edition"),
    ("last-engagement-poll.json", "engagement-poll"),
    ("last-preference-update.json", "preference-update"),
    ("last-linkedin-keepalive.json", "linkedin-keepalive"),
]


def _utcnow() -> datetime:
    """Tiny wrapper so tests can monkeypatch the clock."""
    return datetime.now(timezone.utc)


class NewsDigestProbe(HeartbeatProbe):
    AGENT_ID = "news-digest"
    TITLE = "News Digest"
    EMOJI = "🐛"

    def __init__(
        self,
        workspace: str | None = None,
        brain_dir: str | None = None,
    ) -> None:
        super().__init__(brain_dir=brain_dir)
        self.workspace = workspace or os.path.expanduser(
            "~/.clawford/news-digest-workspace"
        )
        self.linkedin_profile_dir = os.path.join(self.workspace, "linkedin-profile")
        self.preferences_model = os.path.join(
            self.workspace, "preferences", "model.json"
        )
        self.cache_dir = os.path.join(self.workspace, "cache")

    def _check_linkedin_profile(self) -> str:
        """Return 'ok' if the persistent Chromium profile dir exists,
        'missing' otherwise. No deep content check — presence of the
        directory is proof that linkedin-auth.py has run at least once."""
        return "ok" if os.path.isdir(self.linkedin_profile_dir) else "missing"

    def _check_morning_edition(self) -> tuple[str, int]:
        """Return (status, item_count).

        status ∈ {'ok', 'pending', 'missing'}:
          - ok      → cache/ranked-<today>.json exists (load to count items)
          - pending → missing, but current UTC time is before the deadline
          - missing → missing, and we're past the deadline (degraded)
        """
        today = _utcnow().strftime("%Y-%m-%d")
        path = os.path.join(self.cache_dir, f"ranked-{today}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                items = data.get("items", [])
                count = len(items) if isinstance(items, list) else 0
                return "ok", count
            except Exception:
                return "ok", 0

        now = _utcnow()
        if now.hour < MORNING_EDITION_DEADLINE_UTC_HOUR:
            return "pending", 0
        return "missing", 0

    def _read_cron_caches(self) -> tuple[str | None, str | None, str | None]:
        latest_ts = None
        latest_name = None
        latest_summary = None
        for filename, name in CACHE_FILES:
            path = os.path.join(self.cache_dir, filename)
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
                ts_str = data.get("timestamp", "")
                if ts_str and (latest_ts is None or ts_str > latest_ts):
                    latest_ts = ts_str
                    latest_name = name
                    latest_summary = data.get("summary", "")
            except Exception:
                continue
        return latest_ts, latest_name, latest_summary

    def probe(self) -> dict:
        """Pure health probe. No side effects."""
        missing_files: list[str] = []
        if not os.path.exists(self.preferences_model):
            missing_files.append("preferences/model.json")

        linkedin_profile = self._check_linkedin_profile()
        morning_edition, items_count = self._check_morning_edition()
        cache_ts, cache_name, cache_summary = self._read_cron_caches()

        errors: list[str] = []
        if missing_files:
            errors.append(f"missing: {', '.join(missing_files)}")
        if linkedin_profile != "ok":
            errors.append(f"linkedin_profile: {linkedin_profile}")
        if morning_edition == "missing":
            errors.append("morning_edition: ranked-today not produced")

        status = "degraded" if errors else "ok"

        result: dict = {
            "status": status,
            "missing_files": missing_files,
            "linkedin_profile": linkedin_profile,
            "morning_edition": morning_edition,
            "items_count": items_count,
            "last_cron_run": cache_ts,
            "last_cron_name": cache_name,
            "last_cron_result": cache_summary,
        }
        if errors:
            result["alert"] = f"{self.EMOJI} {self.AGENT_ID} degraded: {'; '.join(errors)}"
        return result

    def render_status_md(self, result: dict) -> str:
        now_str = _utcnow().strftime("%Y-%m-%d %H:%M UTC")

        status = result.get("status", "ok")
        last_cron_run = result.get("last_cron_run") or now_str
        last_cron_name = result.get("last_cron_name") or "heartbeat"
        last_cron_result = result.get("last_cron_result") or "heartbeat ran"
        linkedin_profile = result.get("linkedin_profile", "ok")
        morning_edition = result.get("morning_edition", "ok")
        items_count = result.get("items_count", 0)

        errors: list[str] = []
        if result.get("missing_files"):
            errors.append(f"missing: {', '.join(result['missing_files'])}")
        if linkedin_profile != "ok":
            errors.append(f"linkedin_profile: {linkedin_profile}")
        if morning_edition == "missing":
            errors.append("morning_edition: ranked-today not produced")
        error_log = "; ".join(errors) if errors else "none"

        return (
            f"# {self.TITLE} — Status\n\n"
            f"- **last_heartbeat:** {now_str}\n"
            f"- **status:** {status}\n"
            f"- **last_cron_run:** {last_cron_run} — {last_cron_name}\n"
            f"- **last_cron_result:** {last_cron_result}\n"
            f"- **linkedin_profile:** {linkedin_profile}\n"
            f"- **morning_edition:** {morning_edition}\n"
            f"- **items_count:** {items_count}\n"
            f"- **error_log:** {error_log}\n"
        )


# Module-level convenience for fleet-health.py / probe-agent.py which
# calls `from heartbeat import probe; print(json.dumps(probe()))`.
_default_instance = NewsDigestProbe()


def probe() -> dict:
    return _default_instance.probe()


def run() -> dict:
    return _default_instance.run()


def main() -> int:
    return NewsDigestProbe().main()


if __name__ == "__main__":
    sys.exit(main())
