#!/usr/bin/env python3
"""structured-facts-extract.py — Stage 4 of the professional-brain
bootstrap. Reads every archives/*.md under self/archives/ and extracts
typed structured facts into self/facts/<type>.json for programmatic
consumption by Huckle / Murphy / scouting.

Fact types produced in v1:
  - employer            (per-role tenure with dates, reporting line)
  - target_company      (per-search-round target + outcome)
  - strength_theme      (theme + evidence types + across_roles)
  - major_accomplishment (launch / metric / named artifact)
  - tenet_authored      (named framework / operating model)

Runs one parallel LLM call per type. Cheap (~5 calls, ~15K-token prompts).
Idempotent: merges with existing self/facts/<type>.json by id.

Usage:
  python3 structured-facts-extract.py                      # all v1 types
  python3 structured-facts-extract.py --types employer     # subset
  python3 structured-facts-extract.py --dry-run            # show prompts only
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root       # noqa: E402
from agents.shared.llm import infer                      # noqa: E402
from structured_facts_lib import (                       # noqa: E402
    FACT_TYPES,
    build_extraction_prompt,
    merge_facts,
    parse_extraction_response,
)


DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT_S = 300


def _load_archives(archives_dir: Path) -> dict[str, str]:
    """Load every .md file under archives_dir into a {name → content} dict."""
    out: dict[str, str] = {}
    if not archives_dir.exists():
        return out
    for path in sorted(archives_dir.glob("*.md")):
        try:
            out[path.name] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def _load_existing_facts(facts_dir: Path, fact_type: str) -> list[dict]:
    path = facts_dir / f"{fact_type}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict) and "records" in data:
        return data["records"]
    if isinstance(data, list):
        return data
    return []


def _write_facts_atomic(facts_dir: Path, fact_type: str, records: list[dict]) -> Path:
    path = facts_dir / f"{fact_type}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": fact_type,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "records": records,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def _extract_one_type(
    fact_type: str,
    archives: dict[str, str],
    *,
    model: str,
    timeout: int,
) -> tuple[list[dict], str | None, int]:
    """Returns (records, error_or_None, prompt_len)."""
    prompt = build_extraction_prompt(fact_type, archives)
    result = infer(prompt=prompt, model=model, json_mode=True, timeout=timeout)
    if not result.ok:
        return ([], f"LLM failed: {result.error}", len(prompt))
    try:
        records = parse_extraction_response(fact_type, result.text or "")
    except ValueError as e:
        return ([], f"parse failed: {e}", len(prompt))
    return (records, None, len(prompt))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", type=str, default=None,
                    help=f"Comma-separated subset (default: all — {','.join(FACT_TYPES)})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show prompt sizes; skip LLM + writes")
    ap.add_argument("--archives-dir", type=Path, default=None,
                    help="Override self/archives/ location")
    ap.add_argument("--facts-dir", type=Path, default=None,
                    help="Override self/facts/ output location")
    ap.add_argument("--model", type=str, default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    brain = dropbox_brain_root()
    archives_dir = args.archives_dir or (brain / "self" / "archives")
    facts_dir = args.facts_dir or (brain / "self" / "facts")

    archives = _load_archives(archives_dir)
    if not archives:
        print(f"ERROR: no archives found under {archives_dir}", file=sys.stderr)
        print("Run role-archive-synthesize.py first.", file=sys.stderr)
        return 1

    print(f"Archives dir: {archives_dir} ({len(archives)} files)")
    print(f"Facts dir:    {facts_dir}")
    for name in archives:
        print(f"  - {name} ({len(archives[name])} chars)")

    target_types = list(FACT_TYPES)
    if args.types:
        target_types = [t.strip() for t in args.types.split(",") if t.strip()]
        unknown = [t for t in target_types if t not in FACT_TYPES]
        if unknown:
            print(f"ERROR: unknown fact types: {unknown}. Valid: {FACT_TYPES}", file=sys.stderr)
            return 1

    print(f"\nExtracting types: {target_types}")

    if args.dry_run:
        for t in target_types:
            prompt = build_extraction_prompt(t, archives)
            print(f"  [{t:24s}] prompt_chars={len(prompt)}")
        print(json.dumps({"status": "ok", "dry_run": True, "types": target_types}))
        return 0

    # Run extractions in parallel
    print(f"\nRunning extractions with model={args.model}, concurrency={args.concurrency}...")
    results: dict[str, tuple[list[dict], str | None, int]] = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(_extract_one_type, t, archives, model=args.model, timeout=args.timeout): t
            for t in target_types
        }
        for future in as_completed(futures):
            t = futures[future]
            try:
                records, err, prompt_len = future.result()
            except Exception as e:  # noqa: BLE001
                records, err, prompt_len = [], f"worker crashed: {e}", 0
            results[t] = (records, err, prompt_len)
            if err:
                print(f"  [{t:24s}] FAIL: {err}")
            else:
                print(f"  [{t:24s}] OK  records={len(records)}  prompt={prompt_len} chars")

    # Merge + write
    written = 0
    failed = 0
    summary_counts: dict[str, int] = {}
    for t in target_types:
        records, err, _ = results.get(t, ([], "missing result", 0))
        if err:
            failed += 1
            continue
        existing = _load_existing_facts(facts_dir, t)
        merged = merge_facts(existing, records)
        _write_facts_atomic(facts_dir, t, merged)
        summary_counts[t] = len(merged)
        written += 1

    print("\nFinal counts (after merge):")
    for t in sorted(summary_counts):
        print(f"  {t:24s} {summary_counts[t]:4d}")

    print(json.dumps({
        "status": "ok",
        "dry_run": False,
        "written": written,
        "failed": failed,
        "counts": summary_counts,
        "facts_dir": str(facts_dir),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
