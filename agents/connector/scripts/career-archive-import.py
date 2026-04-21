#!/usr/bin/env python3
"""career-archive-import.py — Stage 1 of the professional-brain bootstrap.

Walks the operator's career archive roots (Personal/Bio, Personal/Job Search,
Archive/Amazon, Archive/Example Corp, Archive/LinkedIn, Archive/Twitch),
extracts text from each candidate file, and runs an LLM classifier over
batches of ~10 files. Writes the classified index to
`~/Dropbox/openclaw-backup/self/archive-index.json`.

Idempotent: files whose (path, mtime) already exist in the index are
skipped (no re-extraction, no re-classification). Safe to re-run.

Stage 2 (ingestion: bios → self/bios/, contact_list → contacts-import.md,
etc.) and Stage 3 (per-role synthesis) are separate scripts that consume
archive-index.json. This script only writes the index.

Usage:
  python3 career-archive-import.py --dry-run                # walk only
  python3 career-archive-import.py --dry-run --verbose      # show per-file
  python3 career-archive-import.py --max 20                 # small first pass
  python3 career-archive-import.py --roles airbnb,amazon    # subset
  python3 career-archive-import.py                          # full run

Status envelope printed as the last stdout line per SCRIPT_CONTRACT.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter
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
from career_archive_lib import (                         # noqa: E402
    EXCERPT_CHARS,
    SOURCE_ROOTS,
    build_classification_prompt,
    extract_text,
    iter_file_records,
    merge_records_into_index,
    parse_classification_response,
)
from workflowy_walker import (                           # noqa: E402
    get_nodes_export,
    iter_meeting_records,
)


ALL_SOURCES = ("filesystem", "workflowy")


DEFAULT_BATCH_SIZE = 10
DEFAULT_LLM_TIMEOUT_S = 240
DEFAULT_MODEL = "gpt-5.4-mini"
_FAILURE_MARKERS = (
    "classifier failed:",
    "classifier parse failed:",
    "worker crashed:",
    "index missing from classifier response",
)
DEFAULT_CONCURRENCY = 10

_PROGRESS_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_existing_index(out_path: Path) -> dict:
    if not out_path.exists():
        return {"files": {}}
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"files": {}}


def _write_index_atomic(out_path: Path, index: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)


def _walk_filesystem(roles: list[str] | None, roots_override: dict[str, Path] | None) -> list[dict]:
    """Walk every configured role root and return raw file records."""
    roots = roots_override or SOURCE_ROOTS
    all_records: list[dict] = []
    for role, root in roots.items():
        if roles and role not in roles:
            continue
        count = 0
        for rec in iter_file_records(root, role=role):
            all_records.append(rec)
            count += 1
        print(f"  {role:12s} {count:4d} files   root={root}")
    return all_records


def _walk_workflowy() -> list[dict]:
    """Pull the entire Workflowy tree and emit one record per meeting
    node. Prints a warning and returns [] if the API key is missing or
    the export fails — Workflowy is a best-effort source."""
    print("  fetching Workflowy /nodes-export...")
    nodes = get_nodes_export()
    if not nodes:
        print("  workflowy     0 records (skipped or failed)")
        return []
    records = list(iter_meeting_records(nodes))
    print(f"  workflowy   {len(records):4d} meeting records (from {len(nodes)} total nodes)")
    return records


def _filter_already_classified(
    records: list[dict],
    existing_files: dict,
    *,
    force_classes: set[str] | None = None,
) -> tuple[list[dict], int, int]:
    """Split records into (to_classify, skipped_idempotent, forced_reclassify).

    Records whose existing class is in force_classes are re-extracted and
    re-classified even if their (path, mtime) hasn't changed — used to
    sweep specific buckets with a tightened prompt.
    """
    to_classify: list[dict] = []
    skipped = 0
    forced = 0
    force_classes = force_classes or set()
    for rec in records:
        key = str(rec["path"])
        prev = existing_files.get(key)
        if prev and prev.get("mtime") == rec["mtime"]:
            if force_classes and prev.get("class") in force_classes:
                to_classify.append(rec)
                forced += 1
                continue
            skipped += 1
            continue
        to_classify.append(rec)
    return to_classify, skipped, forced


def _extract_all(records: list[dict], *, verbose: bool = False) -> None:
    """Populate text_excerpt and extractor on each record in place.

    Workflowy records (ext=".wf") already come with text_excerpt from
    the walker; we skip disk extraction and set extractor="workflowy".
    Filesystem records (Path in .path) get routed through extract_text.
    """
    for rec in records:
        if rec.get("ext") == ".wf":
            # Already populated by iter_meeting_records
            excerpt = rec.get("text_excerpt", "") or ""
            rec["text_excerpt"] = excerpt[:EXCERPT_CHARS]
            rec["extractor"] = "workflowy"
            if verbose:
                print(f"    [OK] workflowy  {rec['path']}")
            continue

        text, extractor = extract_text(rec["path"])
        rec["text_excerpt"] = text[:EXCERPT_CHARS] if text else ""
        rec["extractor"] = extractor
        if verbose:
            marker = "OK" if text else "--"
            print(f"    [{marker}] {extractor:10s} {rec['path']}")


def _classify_one_batch(batch: list[dict], *, model: str, timeout: int) -> tuple[list[dict], str | None, str]:
    """Classify a single batch. Returns (classified_records, error_or_None, raw_response).

    Called from a worker thread. No printing here — progress is emitted
    from the main thread as futures complete to keep output readable
    under parallelism.
    """
    prompt = build_classification_prompt(batch)
    result = infer(prompt=prompt, model=model, json_mode=True, timeout=timeout)
    if not result.ok:
        return (
            [_mark_failed(rec, f"classifier failed: {result.error}") for rec in batch],
            f"classifier failed: {result.error}",
            "",
        )

    try:
        parsed = parse_classification_response(result.text)
    except ValueError as e:
        return (
            [_mark_failed(rec, f"classifier parse failed: {e}") for rec in batch],
            f"classifier parse failed: {e}",
            result.text[:500] if result.text else "",
        )

    by_index = {p["index"]: p for p in parsed}
    out: list[dict] = []
    for rec in batch:
        p = by_index.get(rec["index"])
        if p is None:
            out.append(_mark_failed(rec, "index missing from classifier response"))
        else:
            out.append(_finalize(rec, p))
    return out, None, result.text


def _classify_batches(
    records: list[dict],
    *,
    batch_size: int,
    timeout: int,
    model: str,
    concurrency: int,
    verbose: bool = False,
) -> list[dict]:
    """Run the LLM classifier over records in parallel batches."""
    batches: list[list[dict]] = []
    for batch_start in range(0, len(records), batch_size):
        batch = records[batch_start:batch_start + batch_size]
        for i, rec in enumerate(batch):
            rec["index"] = i
        batches.append(batch)

    total = len(batches)
    classified: list[dict] = []
    print(f"  dispatching {total} batches | model={model} | concurrency={concurrency}")

    done_count = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_classify_one_batch, b, model=model, timeout=timeout): b
            for b in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                batch_classified, err, raw = future.result()
            except Exception as e:  # noqa: BLE001 — defensive; future.result should surface cleanly
                batch_classified = [_mark_failed(rec, f"worker crashed: {e}") for rec in batch]
                err = f"worker crashed: {e}"
                raw = ""

            classified.extend(batch_classified)

            with _PROGRESS_LOCK:
                done_count += 1
                if err:
                    print(f"  [{done_count}/{total}] FAIL: {err}")
                    if verbose and raw:
                        print(f"    raw[:200]: {raw[:200]!r}")
                else:
                    counts = Counter(r["class"] for r in batch_classified)
                    top_label = ", ".join(f"{c}={n}" for c, n in counts.most_common(3))
                    print(f"  [{done_count}/{total}] OK  {top_label}")

    return classified


def _finalize(rec: dict, classification: dict) -> dict:
    """Build the final index entry for a successfully-classified record."""
    entry = {
        "path": str(rec["path"]),
        "role": rec["role"],
        "ext": rec["ext"],
        "size": rec["size"],
        "mtime": rec["mtime"],
        "extractor": rec.get("extractor", ""),
        "class": classification["class"],
        "signal_score": classification["signal_score"],
        "summary": classification["summary"],
        "chronological_date": classification.get("chronological_date", ""),
        "date_source": classification.get("date_source", "unknown"),
        "date_confidence": classification.get("date_confidence", 0.0),
        "classified_at": _now_iso(),
    }
    if rec.get("parent_date"):
        entry["parent_date_hint"] = rec["parent_date"]
    return entry


def _mark_failed(rec: dict, reason: str) -> dict:
    entry = {
        "path": str(rec["path"]),
        "role": rec["role"],
        "ext": rec["ext"],
        "size": rec["size"],
        "mtime": rec["mtime"],
        "extractor": rec.get("extractor", ""),
        "class": "skip",
        "signal_score": 0.0,
        "summary": reason,
        "chronological_date": "",
        "date_source": "unknown",
        "date_confidence": 0.0,
        "classified_at": _now_iso(),
    }
    if rec.get("parent_date"):
        entry["parent_date_hint"] = rec["parent_date"]
    return entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Walk and report file counts only — no extraction, no LLM, no writes")
    ap.add_argument("--max", type=int, default=None,
                    help="Cap on total files processed (first N after walk)")
    ap.add_argument("--roles", type=str, default=None,
                    help="Comma-separated subset of filesystem roles (e.g. airbnb,amazon)")
    ap.add_argument("--sources", type=str, default=",".join(ALL_SOURCES),
                    help=f"Comma-separated sources to include. Options: {','.join(ALL_SOURCES)}. "
                         f"Default: both.")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--timeout", type=int, default=DEFAULT_LLM_TIMEOUT_S,
                    help="LLM call timeout in seconds per batch")
    ap.add_argument("--model", type=str, default=DEFAULT_MODEL,
                    help=f"LLM model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"Parallel classifier workers (default: {DEFAULT_CONCURRENCY})")
    ap.add_argument("--force-classes", type=str, default=None,
                    help="Comma-separated list of classes. Records whose current class "
                         "matches will be re-classified even if unchanged on disk. "
                         "Use after tightening the classifier prompt to sweep specific buckets.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Override output path (default: ~/Dropbox/openclaw-backup/self/archive-index.json)")
    ap.add_argument("--verbose", action="store_true",
                    help="Print per-file extraction + per-classification details")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    roles = None
    if args.roles:
        roles = [r.strip() for r in args.roles.split(",") if r.strip()]

    sources = {s.strip() for s in args.sources.split(",") if s.strip()}
    unknown_sources = sources - set(ALL_SOURCES)
    if unknown_sources:
        print(f"ERROR: unknown --sources: {unknown_sources}. Valid: {ALL_SOURCES}", file=sys.stderr)
        return 1

    out_path = args.out or (dropbox_brain_root() / "self" / "archive-index.json")

    print(f"Output: {out_path}")
    print(f"Sources: {sorted(sources)}")
    all_records: list[dict] = []
    if "filesystem" in sources:
        print("Walking filesystem archive roots:")
        all_records.extend(_walk_filesystem(roles, roots_override=None))
    if "workflowy" in sources:
        print("Walking Workflowy:")
        all_records.extend(_walk_workflowy())
    print(f"Total candidate records: {len(all_records)}")

    if args.max is not None and args.max < len(all_records):
        print(f"Capping at --max {args.max} (dropping {len(all_records) - args.max})")
        all_records = all_records[:args.max]

    existing = _load_existing_index(out_path)
    existing_files = existing.get("files") or {}

    force_classes = None
    if args.force_classes:
        force_classes = {c.strip() for c in args.force_classes.split(",") if c.strip()}
        print(f"Force re-classifying current classes: {sorted(force_classes)}")

    to_classify, skipped_idempotent, forced = _filter_already_classified(
        all_records, existing_files, force_classes=force_classes
    )
    msg = f"To classify: {len(to_classify)}  (skipped {skipped_idempotent} unchanged"
    if forced:
        msg += f", forced {forced} via --force-classes"
    msg += ")"
    print(msg)

    if args.dry_run:
        # Stable preview: report what WOULD happen
        by_ext: Counter = Counter(r["ext"] for r in to_classify)
        by_role: Counter = Counter(r["role"] for r in to_classify)
        print("\nWould classify by extension:")
        for ext, n in sorted(by_ext.items()):
            print(f"  {ext or '(none)':10s} {n:4d}")
        print("Would classify by role:")
        for role, n in sorted(by_role.items()):
            print(f"  {role:12s} {n:4d}")
        print()
        print(json.dumps({
            "status": "ok",
            "dry_run": True,
            "total_candidates": len(all_records),
            "to_classify": len(to_classify),
            "skipped_idempotent": skipped_idempotent,
            "out": str(out_path),
        }))
        return 0

    if not to_classify:
        print("Nothing new to classify.")
        print(json.dumps({
            "status": "ok",
            "dry_run": False,
            "total_candidates": len(all_records),
            "to_classify": 0,
            "skipped_idempotent": skipped_idempotent,
            "out": str(out_path),
        }))
        return 0

    print("\nExtracting text from files...")
    _extract_all(to_classify, verbose=args.verbose)

    print(f"\nClassifying {len(to_classify)} files in batches of {args.batch_size}...")
    classified = _classify_batches(
        to_classify,
        batch_size=args.batch_size,
        timeout=args.timeout,
        model=args.model,
        concurrency=args.concurrency,
        verbose=args.verbose,
    )

    # Filter out failed records — don't poison the index with transient
    # failures. They'll get re-tried on the next run.
    successes = [r for r in classified if not any(r["summary"].startswith(m) for m in _FAILURE_MARKERS)]
    failures = len(classified) - len(successes)
    if failures:
        print(f"\n{failures} records failed classification; NOT written to index (will retry on next run)")

    # Snapshot current index before overwriting when force-reclassifying —
    # lets the operator diff before/after on the specific buckets.
    if force_classes and out_path.exists():
        backup = out_path.with_suffix(out_path.suffix + ".bak")
        backup.write_bytes(out_path.read_bytes())
        print(f"Snapshot saved to {backup}")

    # Merge into existing index and write atomically. overwrite=True when
    # forcing re-classification so same-mtime records are overwritten.
    merged = merge_records_into_index(existing, successes, overwrite=bool(force_classes))
    _write_index_atomic(out_path, merged)

    # Summary
    by_class: Counter = Counter(r["class"] for r in successes)
    print("\nResults by class (successful only):")
    for cls, n in sorted(by_class.items(), key=lambda kv: -kv[1]):
        print(f"  {cls:24s} {n:4d}")

    print(json.dumps({
        "status": "ok",
        "dry_run": False,
        "total_candidates": len(all_records),
        "classified_ok": len(successes),
        "classified_failed": failures,
        "skipped_idempotent": skipped_idempotent,
        "by_class": dict(by_class),
        "out": str(out_path),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
