#!/usr/bin/env python3
"""workflowy-facts-mine.py — mine durable facts from the operator's Workflowy notes.

Daily cron. Pulls the Workflowy tree via REST (/nodes-export), walks
nodes looking for mentions of known-people by full_name, and runs each
matching node's text through the shared fact extractor.

Unlike Gmail (From/To addresses) and Krisp (meeting attendees),
Workflowy has no explicit subject — subjects are INFERRED from
whole-word name mentions. To keep signal-to-noise reasonable, nodes
with zero matched names skip the LLM call entirely.

Safety: defaults to dry-run. Pass ``--commit`` to write. Missing
WORKFLOWY_API_KEY → status=degraded (not error).

Usage:
  python3 workflowy-facts-mine.py --commit
  python3 workflowy-facts-mine.py --max-nodes 200 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root                 # noqa: E402
from agents.shared.fact_extraction import (                        # noqa: E402
    append_pending_review,
    extract_facts_from_text,
)
from agents.shared.facts import upsert_fact                        # noqa: E402
from workflowy_facts_mine_lib import (                             # noqa: E402
    build_name_to_slug_index,
    find_mentioned_slugs,
)


DEFAULT_CURSOR = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/cache/workflowy-mine-cursor.json"
))

WORKFLOWY_BASE_URL = "https://workflowy.com/api/v1"


def _get_api_key() -> str:
    """Read WORKFLOWY_API_KEY from env or from the workspace .env file."""
    key = os.environ.get("WORKFLOWY_API_KEY", "").strip()
    if key:
        return key
    env_candidates = [
        Path(os.path.expanduser("~/.clawford/connector-workspace/.env")),
        Path(os.path.expanduser("~/clawford/.env")),
        Path("/home/openclaw/clawford/.env"),
    ]
    for env_file in env_candidates:
        if not env_file.exists():
            continue
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("WORKFLOWY_API_KEY=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return ""


def _fetch_nodes(api_key: str, max_nodes: int) -> list[dict]:
    """Hit /nodes-export and return the first ``max_nodes`` node dicts.
    Each node has at minimum {'id': str, 'name': str}."""
    req = urllib.request.Request(
        f"{WORKFLOWY_BASE_URL}/nodes-export",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    nodes: list[dict]
    if isinstance(data, dict):
        nodes = data.get("nodes") or data.get("items") or []
    elif isinstance(data, list):
        nodes = data
    else:
        nodes = []
    return nodes[:max_nodes]


def run(
    *,
    api_key: str,
    people_dir: Path,
    facts_dir: Path,
    cursor_path: Path,
    max_nodes: int,
    commit: bool,
    verbose: bool = False,
) -> dict:
    """Run one miner pass over the Workflowy tree."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stats = {
        "status": "ok",
        "nodes_scanned": 0,
        "nodes_skipped_no_mentions": 0,
        "facts_minted": 0,
        "skipped_dup": 0,
        "facts_flagged_low_conf": 0,
        "extract_errors": 0,
    }

    if not api_key:
        stats["status"] = "degraded"
        stats["reason"] = "WORKFLOWY_API_KEY not set — skipping Workflowy mine"
        return stats

    name_to_slug = build_name_to_slug_index(people_dir)
    nodes = _fetch_nodes(api_key, max_nodes)

    for node in nodes:
        stats["nodes_scanned"] += 1
        text = (node.get("name") or "").strip()
        if not text:
            stats["nodes_skipped_no_mentions"] += 1
            continue

        slugs = find_mentioned_slugs(text, name_to_slug)
        if not slugs:
            stats["nodes_skipped_no_mentions"] += 1
            continue

        node_id = str(node.get("id") or "")
        meta = {
            "source": "workflowy",
            "node_id": node_id,
        }

        try:
            facts = extract_facts_from_text(
                text=text,
                source_context=meta,
                candidate_slugs=slugs,
            )
        except Exception:
            stats["extract_errors"] += 1
            if verbose:
                traceback.print_exc(file=sys.stderr)
            continue

        for f in facts:
            if commit:
                result = upsert_fact(
                    facts_dir=facts_dir,
                    subject=f["subject"],
                    category=f["category"],
                    content=f["content"],
                    source_agent="connector",
                    source_type="mined",
                    source_detail=f["source_detail"],
                    confidence=f["confidence"],
                    idempotency_key=f["idempotency_key"],
                    recorded_at=now_iso,
                    audience_scope=f["audience_scope"],
                )
                if result["status"] == "skipped":
                    stats["skipped_dup"] += 1
                    continue
                stats["facts_minted"] += 1
                if f.get("needs_review"):
                    append_pending_review(
                        facts_dir,
                        {**f, "id": result["id"]},
                    )
                    stats["facts_flagged_low_conf"] += 1
            else:
                stats["facts_minted"] += 1
                if f.get("needs_review"):
                    stats["facts_flagged_low_conf"] += 1

    if commit:
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"last_run_at": now_iso}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(cursor_path)

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor-path", type=Path, default=DEFAULT_CURSOR)
    ap.add_argument("--people-dir", type=Path, default=None)
    ap.add_argument("--facts-dir", type=Path, default=None)
    ap.add_argument("--max-nodes", type=int, default=500)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    brain = dropbox_brain_root()
    people_dir = args.people_dir or (brain / "people")
    facts_dir = args.facts_dir or (brain / "facts")

    commit = args.commit and not args.dry_run
    api_key = _get_api_key()

    try:
        result = run(
            api_key=api_key,
            people_dir=people_dir,
            facts_dir=facts_dir,
            cursor_path=args.cursor_path,
            max_nodes=args.max_nodes,
            commit=commit,
            verbose=args.verbose,
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print()
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 0

    print()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
