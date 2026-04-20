#!/usr/bin/env python3
"""facts-scope-augment.py — one-time pass to tag Huckle-native facts with
audience_scope so the confidentiality filter has something to work with.

Flux-imported facts already carry audience_scope. Huckle's own facts
(written before the Flux import) do not — they're currently treated as
visible-to-all (Flux default). This script classifies them via LLM and
rewrites facts/YYYY-MM.md files in place.

Flow:
  1. Load all facts across facts/*.md; filter to those without
     audience_scope.
  2. Split into batches (default 20 per LLM call).
  3. Call codex per batch with the scope classifier prompt.
  4. Parse + merge per-fact scopes.
  5. --dry-run: writes the proposed map to cache/scope-review.json (no
     facts files touched).
  6. --apply: rewrites facts/*.md in place, atomic per-file. Idempotent.

Usage:
  python3 facts-scope-augment.py --dry-run
  python3 facts-scope-augment.py --apply
  python3 facts-scope-augment.py --apply --max 50   # cap for safe rollout
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root                 # noqa: E402
from agents.shared.llm import infer                                # noqa: E402
from facts_scope_augment_lib import (                              # noqa: E402
    build_scope_classifier_prompt,
    load_untagged_facts,
    parse_scope_response,
    rewrite_facts_file_with_scopes,
)


DEFAULT_REVIEW = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/cache/scope-review.json"
))


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def call_codex(prompt: str, timeout: int = 120) -> str:
    result = infer(prompt=prompt, json_mode=True, timeout=timeout)
    if not result.ok:
        return json.dumps({"error": f"codex infer failed: {result.error}"})
    return result.text or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-dir", type=Path,
                    default=None,
                    help="Override brain/facts path (default: brain facts dir)")
    ap.add_argument("--review-json", type=Path, default=DEFAULT_REVIEW)
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--max", type=int, help="Cap on facts classified")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="Write scopes to facts files (default is dry-run)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    if args.apply and args.dry_run:
        print("ERROR: --apply and --dry-run are mutually exclusive", file=sys.stderr)
        return 1
    if not args.apply:
        args.dry_run = True

    facts_dir = args.facts_dir or (dropbox_brain_root() / "facts")
    print(f"Facts dir: {facts_dir}")

    untagged = load_untagged_facts(facts_dir)
    print(f"Untagged facts: {len(untagged)}")
    if args.max:
        untagged = untagged[: args.max]
        print(f"Capped to: {len(untagged)}")
    if not untagged:
        print("Nothing to do.")
        return 0

    # Classify in batches
    scope_map: dict[str, list[str]] = {}
    for batch_idx, batch in enumerate(chunks(untagged, args.batch_size), 1):
        fact_ids = {f["id"] for f in batch}
        print(f"Batch {batch_idx}: classifying {len(batch)} facts...")
        prompt = build_scope_classifier_prompt(batch)
        llm_text = call_codex(prompt)
        batch_scopes = parse_scope_response(llm_text, fact_ids=fact_ids)
        scope_map.update(batch_scopes)
        if args.verbose:
            for fid, tags in batch_scopes.items():
                content_snip = next((f["content"][:80] for f in batch if f["id"] == fid), "")
                print(f"  {fid}: {tags}  ({content_snip})")

    print()
    print(f"Total classified: {len(scope_map)} / {len(untagged)}")

    if args.dry_run:
        args.review_json.parent.mkdir(parents=True, exist_ok=True)
        args.review_json.write_text(
            json.dumps({"scope_map": scope_map, "untagged_total": len(untagged)},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Wrote proposed scopes to {args.review_json}")
        print("Re-run with --apply to write into facts/*.md")
        print(json.dumps({"status": "ok", "dry_run": True,
                          "classified": len(scope_map), "untagged": len(untagged)}))
        return 0

    # Apply: group by source file, rewrite each atomically
    by_file: dict[str, list[str]] = {}
    for f in untagged:
        if f["id"] in scope_map:
            by_file.setdefault(f["_source_path"], []).append(f["id"])

    total_updates = 0
    for file_path, ids in by_file.items():
        per_file_scope = {fid: scope_map[fid] for fid in ids}
        updates = rewrite_facts_file_with_scopes(Path(file_path), per_file_scope)
        print(f"  {file_path}: {updates} facts updated")
        total_updates += updates

    print()
    print(json.dumps({"status": "ok", "dry_run": False,
                      "classified": len(scope_map), "applied": total_updates}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
