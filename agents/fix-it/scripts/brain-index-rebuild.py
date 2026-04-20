#!/usr/bin/env python3
"""brain-index-rebuild.py — nightly brain/facts/_index.json rebuilder.

Registered under Mr Fixit because (a) he's exempt from bwrap and can
see the shared brain unconditionally and (b) he already owns
fleet-level brain hygiene crons (monthly-archival, workspace-snapshot-
check, brain-validation-check). Runs after the 6h miner window's last
pre-brief slot (2:00 AM PT) and before the morning brief-gen (3:30 AM
PT).

The index is a HINT for agents/shared/facts::load_facts_for_subject —
when present + fresh, readers open only the months that contain facts
for the subject being drafted. Staleness is detected by mtime
comparison against monthly files; a stale index falls back to the
slow full-scan path automatically.

SCRIPT_CONTRACT-compliant.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from agents.shared.brain import dropbox_brain_root               # noqa: E402
from agents.shared.brain_index import rebuild_index, save_index  # noqa: E402


def run() -> dict:
    start = datetime.now(timezone.utc)
    facts_dir = dropbox_brain_root() / "facts"
    if not facts_dir.exists():
        return {
            "status": "degraded",
            "reason": "facts dir absent",
            "facts_dir": str(facts_dir),
        }

    index = rebuild_index(facts_dir)
    path = save_index(facts_dir, index)

    subject_count = len(index.get("by_subject") or {})
    entry_count = sum(
        len(v) for v in (index.get("by_subject") or {}).values()
    )
    duration_ms = int(
        (datetime.now(timezone.utc) - start).total_seconds() * 1000
    )
    return {
        "status": "ok",
        "index_path": str(path),
        "subjects_indexed": subject_count,
        "entries_indexed": entry_count,
        "duration_ms": duration_ms,
        "built_at": index.get("built_at"),
    }


def main() -> int:
    try:
        result = run()
    except Exception as exc:
        result = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
    print(json.dumps(result))
    return 0  # SCRIPT_CONTRACT: always exit 0, signal via JSON


if __name__ == "__main__":
    sys.exit(main())
