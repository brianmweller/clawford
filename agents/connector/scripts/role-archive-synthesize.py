#!/usr/bin/env python3
"""role-archive-synthesize.py — Stage 3 of the professional-brain
bootstrap. Reads the Stage 1 archive-index.json plus the hand-edited
role-timeline.md, groups records by synthesis role, ranks them by
class × signal_score, and asks the LLM to synthesize one markdown file
per role under ~/Dropbox/openclaw-backup/self/archives/<role>.md.

Output sections (per role): Scope & team, OKRs and metrics owned,
Tenets/frameworks/operating models, Accomplishments, Feedback themes,
Departure context.

Confidentiality: the prompt forbids verbatim proprietary strategy and
identifiable feedback about named individuals — only aggregated
themes. the operator reviews each archive file before the downstream profile
synthesis reads from it.

Usage:
  python3 role-archive-synthesize.py                           # all roles
  python3 role-archive-synthesize.py --roles airbnb,linkedin   # subset
  python3 role-archive-synthesize.py --dry-run                 # show ranking only
  python3 role-archive-synthesize.py --limit 30                # records per role
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
from role_archive_lib import (                           # noqa: E402
    DEFAULT_LIMIT,
    DEFAULT_RAW_CHARS,
    DEFAULT_RAW_LIMIT,
    build_meeting_patterns_prompt,
    build_synthesis_prompt,
    build_workflowy_text_map,
    fetch_raw_content,
    group_records_by_role,
    parse_meeting_patterns_response,
    pick_raw_records,
    rank_records,
)
from role_timeline_lib import parse_role_timeline        # noqa: E402
from workflowy_walker import get_nodes_export            # noqa: E402


DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT_S = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_archive_markdown(out_path: Path, role: str, range_info: dict, body_md: str, record_count: int) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {role.upper()} — {range_info['label']}\n\n"
        f"- **slug:** {role}\n"
        f"- **start:** {range_info['start'] or '(before timeline)'}\n"
        f"- **end:** {range_info['end']}\n"
        f"- **evidence_records:** {record_count}\n"
        f"- **generated_at:** {_now_iso()}\n\n"
        f"---\n\n"
    )
    out_path.write_text(header + body_md.strip() + "\n", encoding="utf-8")


def _extract_meeting_patterns(
    role_label: str,
    meeting_records: list[dict],
    *,
    model: str,
    timeout: int,
) -> dict | None:
    """Run the meeting-patterns pre-pass LLM call. Returns the parsed
    dict or None if no meeting records / LLM failure (synthesis will
    proceed without patterns in that case)."""
    if not meeting_records:
        return None
    prompt = build_meeting_patterns_prompt(role_label, meeting_records)
    result = infer(prompt=prompt, model=model, timeout=timeout, json_mode=True)
    if not result.ok:
        return None
    return parse_meeting_patterns_response(result.text or "")


def synthesize_one_role(
    range_info: dict,
    records: list[dict],
    *,
    model: str,
    timeout: int,
    limit: int,
    raw_limit: int = DEFAULT_RAW_LIMIT,
    raw_chars: int = DEFAULT_RAW_CHARS,
    workflowy_texts: dict[str, str] | None = None,
    meeting_records_for_patterns: list[dict] | None = None,
) -> tuple[str, str | None, dict]:
    """Synthesize one role's archives/<role>.md content (v2).

    Returns (body_md, error, metadata) where metadata is:
      {"raw_count", "summary_count", "patterns_present", "total_ranked"}
    """
    meta = {"raw_count": 0, "summary_count": 0, "patterns_present": False, "total_ranked": 0}
    ranked = rank_records(records, limit=limit)
    meta["total_ranked"] = len(ranked)
    if not ranked:
        return ("(no records for this role)\n", None, meta)

    # Split top records by class for raw-content fetching
    raw_records, summary_records = pick_raw_records(ranked, raw_limit=raw_limit)

    # Fetch raw content for the top records
    raw_with_content: list[tuple[dict, str]] = []
    for rec in raw_records:
        content = fetch_raw_content(rec, workflowy_texts=workflowy_texts, max_chars=raw_chars)
        # Even if content is empty, still pass a placeholder so the LLM
        # knows this record was in the raw tier but extraction failed
        if not content:
            content = f"(raw content unavailable for this record; classifier summary: {rec.get('summary', '')[:200]})"
        raw_with_content.append((rec, content))

    meta["raw_count"] = len(raw_with_content)
    meta["summary_count"] = len(summary_records)

    # Extract meeting patterns from this role's meeting records (pre-pass)
    patterns = None
    if meeting_records_for_patterns:
        patterns = _extract_meeting_patterns(
            range_info.get("label", range_info.get("role", "")),
            meeting_records_for_patterns,
            model=model, timeout=timeout,
        )
        if patterns is not None:
            meta["patterns_present"] = True

    prompt = build_synthesis_prompt(
        range_info,
        raw_content_records=raw_with_content,
        summary_records=summary_records,
        meeting_patterns=patterns,
    )
    result = infer(prompt=prompt, model=model, timeout=timeout, json_mode=False)
    if not result.ok:
        return ("", f"LLM failed: {result.error}", meta)
    return (result.text or "", None, meta)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roles", type=str, default=None,
                    help="Comma-separated subset of roles to synthesize (default: all in timeline)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show record counts and ranking; skip LLM and writes")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"Max evidence records per role sent to LLM (default: {DEFAULT_LIMIT})")
    ap.add_argument("--raw-limit", type=int, default=DEFAULT_RAW_LIMIT,
                    help=f"Top N records to fetch raw content for (default: {DEFAULT_RAW_LIMIT})")
    ap.add_argument("--raw-chars", type=int, default=DEFAULT_RAW_CHARS,
                    help=f"Max chars of raw content per record (default: {DEFAULT_RAW_CHARS})")
    ap.add_argument("--skip-workflowy-fetch", action="store_true",
                    help="Don't re-fetch Workflowy; raw content for wf:// records won't be available")
    ap.add_argument("--skip-meeting-patterns", action="store_true",
                    help="Skip the per-role meeting-patterns LLM pre-pass")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="Parallel role synthesis workers (default: 4)")
    ap.add_argument("--model", type=str, default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--index", type=Path, default=None,
                    help="Override archive-index.json path")
    ap.add_argument("--timeline", type=Path, default=None,
                    help="Override role-timeline.md path")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Override self/archives/ output dir")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    brain = dropbox_brain_root()
    index_path = args.index or (brain / "self" / "archive-index.json")
    timeline_path = args.timeline or (brain / "self" / "role-timeline.md")
    search_timeline_path = brain / "self" / "search-timeline.md"
    out_dir = args.out_dir or (brain / "self" / "archives")

    if not index_path.exists():
        print(f"ERROR: archive-index not found: {index_path}", file=sys.stderr)
        print("Run career-archive-import.py first.", file=sys.stderr)
        return 1
    if not timeline_path.exists():
        print(f"ERROR: role-timeline not found: {timeline_path}", file=sys.stderr)
        return 1

    print(f"Index:           {index_path}")
    print(f"Role timeline:   {timeline_path}")
    print(f"Search timeline: {search_timeline_path if search_timeline_path.exists() else '(not present — flat job-search bucket)'}")
    print(f"Out dir:         {out_dir}")

    ranges = parse_role_timeline(timeline_path)
    print(f"\nRole timeline ({len(ranges)} roles):")
    for r in ranges:
        print(f"  {r['role']:24s} {r['start'] or '(before)':10s} → {r['end']:10s}  {r['label']}")

    search_ranges = None
    if search_timeline_path.exists():
        search_ranges = parse_role_timeline(search_timeline_path)
        print(f"\nSearch timeline ({len(search_ranges)} rounds):")
        for r in search_ranges:
            print(f"  {r['role']:24s} {r['start'] or '(before)':10s} → {r['end']:10s}  {r['label']}")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    print(f"\nLoaded index: {len(index.get('files') or {})} files")

    groups = group_records_by_role(index, ranges, search_timeline_ranges=search_ranges)
    print("\nRecords per synthesis role (pre-ranking):")
    for role in sorted(groups):
        print(f"  {role:14s} {len(groups[role]):5d}")

    # Determine which roles to synthesize. By default: every role in
    # the role timeline PLUS every search round that has records. Skip
    # pre-twitch (low evidence) unless explicitly requested.
    target_roles = []
    all_ranges = list(ranges)
    if search_ranges:
        all_ranges.extend(search_ranges)
    range_by_role = {r["role"]: r for r in all_ranges}

    if args.roles:
        target_roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    else:
        for r in all_ranges:
            if r["role"] in {"pre-twitch"}:
                continue
            if groups.get(r["role"]):
                target_roles.append(r["role"])

    print(f"\nWill synthesize ({len(target_roles)}): {target_roles}")

    if args.dry_run:
        print()
        for role in target_roles:
            records = groups.get(role, [])
            ranked = rank_records(records, limit=args.limit)
            print(f"=== {role} ({len(ranked)} top-ranked of {len(records)}) ===")
            for r in ranked[:10]:
                date = r.get("chronological_date") or "?"
                cls = r.get("class", "?")
                score = r.get("signal_score", 0.0)
                name = Path(r["path"]).name if not r["path"].startswith("wf://") else r["path"][:30] + "..."
                print(f"  [{cls:22s} s={score:.2f} d={date:10s}] {name}")
            if len(ranked) > 10:
                print(f"  ... and {len(ranked) - 10} more")
            print()
        print(json.dumps({"status": "ok", "dry_run": True, "roles": target_roles}))
        return 0

    # Fetch Workflowy once for raw-content lookup (shared across roles)
    workflowy_texts: dict[str, str] = {}
    if not args.skip_workflowy_fetch:
        print("\nFetching Workflowy /nodes-export for raw-content lookup...")
        nodes = get_nodes_export()
        if nodes:
            workflowy_texts = build_workflowy_text_map(nodes, max_chars=args.raw_chars)
            print(f"  Built text map for {len(workflowy_texts)} meeting records")
        else:
            print("  WARN: no nodes returned — wf:// raw content will be placeholder-only")

    # For meeting-patterns pre-pass: include Workflowy records (role=
    # meetings at Stage 1) AND filesystem records whose class is
    # meeting-shaped (1:1 notes, meeting minutes, coaching docs). the operator's
    # pre-Workflowy career (Twitch) has rich 1:1 archives on disk in
    # Archive/<company>/Keep Notes/ etc. that would otherwise miss the
    # pre-pass.
    meeting_classes = {"email_or_correspondence", "feedback_given", "feedback_received"}
    meetings_by_role: dict[str, list[dict]] = {}
    if not args.skip_meeting_patterns:
        for role in target_roles:
            meetings_by_role[role] = [
                rec for rec in groups.get(role, [])
                if str(rec.get("path", "")).startswith("wf://")
                or rec.get("class") in meeting_classes
            ]
            if meetings_by_role[role]:
                print(f"  {role:20s} {len(meetings_by_role[role]):5d} meeting records for patterns pre-pass")

    # Synthesize each role in parallel
    def _task(role: str):
        range_info = range_by_role.get(role)
        if not range_info:
            return role, "", f"role {role!r} not in timeline", {}
        records = groups.get(role, [])
        meetings_for_patterns = meetings_by_role.get(role, [])
        body, err, meta = synthesize_one_role(
            range_info, records,
            model=args.model, timeout=args.timeout, limit=args.limit,
            raw_limit=args.raw_limit, raw_chars=args.raw_chars,
            workflowy_texts=workflowy_texts if workflowy_texts else None,
            meeting_records_for_patterns=meetings_for_patterns or None,
        )
        return role, body, err, meta

    print(f"\nSynthesizing with model={args.model}, concurrency={args.concurrency}...")
    results: dict[str, tuple[str, str | None, dict]] = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(_task, role): role for role in target_roles}
        for future in as_completed(futures):
            role = futures[future]
            try:
                _, body, err, meta = future.result()
            except Exception as e:  # noqa: BLE001
                body, err, meta = "", f"worker crashed: {e}", {}
            results[role] = (body, err, meta)
            if err:
                print(f"  [{role:20s}] FAIL: {err}")
            else:
                pat = "patterns" if meta.get("patterns_present") else "no-patterns"
                print(f"  [{role:20s}] OK  raw={meta.get('raw_count', 0)} "
                      f"summary={meta.get('summary_count', 0)} {pat} ({len(body)} chars)")

    # Write archives
    written = 0
    failed = 0
    for role in target_roles:
        body, err, meta = results.get(role, ("", "missing result", {}))
        if err:
            failed += 1
            continue
        range_info = range_by_role[role]
        out_path = out_dir / f"{role}.md"
        _write_archive_markdown(out_path, role, range_info, body, meta.get("total_ranked", 0))
        written += 1

    # Status envelope
    print()
    print(json.dumps({
        "status": "ok",
        "written": written,
        "failed": failed,
        "roles": target_roles,
        "out_dir": str(out_dir),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
