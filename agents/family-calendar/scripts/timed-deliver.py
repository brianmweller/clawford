#!/usr/bin/env python3
"""timed-deliver.py — family-calendar's delivery shim.

Thin wrapper around agents/shared/telegram_api.py. All the real logic
(argv parsing, message chunking, gather-window timing, Telegram API
calls) lives in the shared module. This file exists only to give the
per-agent cron path something to invoke without reconfiguring cron
entries.

Before Phase 2 this file was 168 lines, byte-identical to four other
agents' copies. After consolidation it's a three-line import+call.

Usage: python3 timed-deliver.py <message_file> [--silent] [--token-env ENV_VAR]
"""

import sys
from pathlib import Path

# --- shared library sys.path shim ---
# Find the first ancestor containing agents/shared/ and prepend it to
# sys.path so `from agents.shared import X` resolves in both the local
# repo layout and the deployed <workspace>/agents/shared/ layout.
# See agents/shared/deploy.py::sync_shared_library.
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.telegram_api import cli_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
