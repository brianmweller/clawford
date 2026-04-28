#!/usr/bin/env python3
"""synthesize_alert.py — choose Telegram alert text for a contract envelope.

Used by ops/scripts/script-contract-host.sh. Closes the silent-fail gap
that hid Huckle Cat's invalid_grant failures for 16 hours: connector
scripts (inbox-triage, gmail-facts-mine, etc.) returned status=error
with the RefreshError tucked inside `wrapped.stderr_tail` and no
`alert` field, so the wrapper logged-but-didn't-page until the daily
gmail-watch-renew finally surfaced the issue.

Resolution order:

  1. Explicit `alert`   (SCRIPT_CONTRACT v2)
  2. Legacy `message`   (v1, kept for back-compat)
  3. Synthesized auth-failure alert when (1)+(2) absent and stderr_tail
     or top-level `error` matches an OAuth refresh-failure pattern.

Status=ok always returns empty (no relay on success).

Invocation:

  python3 synthesize_alert.py <json-line> [<logname>]

Exit code is always 0; stdout is the alert text (empty = no relay).
The bash wrapper calls this once per cron tick, so any change to
Telegram-alert behavior is one Python edit + one test run away.
"""
from __future__ import annotations

import json
import re
import sys


_AUTH_PATTERN = re.compile(
    r"invalid_grant"
    r"|RefreshError"
    r"|Token has been expired or revoked"
    r"|invalid_credentials",
    re.IGNORECASE,
)


def synthesize(envelope: dict, logname: str = "") -> str:
    """Return the Telegram alert text for `envelope`, or empty string.

    Pure function: no side effects, no I/O. All the precedence and
    pattern-matching logic lives here so the test suite can pin
    behavior without spawning subprocesses.
    """
    if envelope.get("status") == "ok":
        return ""

    explicit = envelope.get("alert") or envelope.get("message")
    if explicit:
        return str(explicit)

    wrapped = envelope.get("wrapped") or {}
    stderr_tail = wrapped.get("stderr_tail") or ""
    error = envelope.get("error") or ""
    haystack = f"{stderr_tail}\n{error}"

    if _AUTH_PATTERN.search(haystack):
        prefix = f"{logname}: " if logname else ""
        return (
            f"\U0001F512 {prefix}OAuth refresh failed — "
            f"token revoked or expired, re-auth needed"
        )

    return ""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    line = argv[1]
    logname = argv[2] if len(argv) > 2 else ""

    try:
        envelope = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return 0

    if not isinstance(envelope, dict):
        return 0

    text = synthesize(envelope, logname)
    if text:
        # Use buffer so unicode (emoji prefix) survives Windows test
        # harnesses where stdout encoding default isn't utf-8.
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, Exception):
            pass
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
