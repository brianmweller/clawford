#!/usr/bin/env python3
"""profile-synthesize.py — Stage 1.2 of the professional-brain bootstrap.

Reads:
  self/archives/*.md     (11 role + search-round archives from Stage 3)
  self/facts/*.json      (5 structured fact types from Stage 4)
  self/archive-index.json (to find priority_override=True records for
                           raw-content attachment)

Produces:
  self/profile.md        — canonical narrative profile

Single LLM call. Output is the actual artifact Huckle and Murphy
consume at compose/prep time.

Usage:
  python3 profile-synthesize.py                 # full synthesis
  python3 profile-synthesize.py --dry-run       # show prompt size only
  python3 profile-synthesize.py --out <path>    # override output path
"""
from __future__ import annotations

import argparse
import json
import sys
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
from career_archive_lib import extract_text              # noqa: E402
from profile_synthesize_lib import build_profile_prompt  # noqa: E402
from role_archive_lib import fetch_raw_content           # noqa: E402


DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT_S = 300
PRIORITY_RAW_CHARS = 15000   # larger than archive synthesis since this is the final layer


def _load_archives(archives_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not archives_dir.exists():
        return out
    for path in sorted(archives_dir.glob("*.md")):
        try:
            out[path.name] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def _load_facts(facts_dir: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not facts_dir.exists():
        return out
    for path in sorted(facts_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        fact_type = path.stem
        if isinstance(data, dict) and "records" in data:
            out[fact_type] = data["records"]
        elif isinstance(data, list):
            out[fact_type] = data
    return out


def _load_priority_raw_docs(index_path: Path, max_chars: int = PRIORITY_RAW_CHARS) -> list[tuple[str, str]]:
    """Find all records in archive-index.json with priority_override=True
    and fetch their raw content."""
    if not index_path.exists():
        return []
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    files = index.get("files") or {}
    out: list[tuple[str, str]] = []
    for path, entry in files.items():
        if not entry.get("priority_override"):
            continue
        rec = dict(entry)
        rec["path"] = path
        content = fetch_raw_content(rec, workflowy_texts=None, max_chars=max_chars)
        if content:
            out.append((path, content))
    return out


def _write_profile_atomic(out_path: Path, body_md: str, stats: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Sam Smith — Professional Profile\n\n"
        f"- **generated_at:** {datetime.now(timezone.utc).isoformat()}\n"
        f"- **archives_read:** {stats.get('archives_count', 0)}\n"
        f"- **fact_types:** {', '.join(sorted(stats.get('fact_types', [])))}\n"
        f"- **priority_raw_docs:** {stats.get('priority_raw_count', 0)}\n\n"
        "---\n\n"
    )
    content = header + body_md.strip() + "\n"
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(out_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archives-dir", type=Path, default=None)
    ap.add_argument("--facts-dir", type=Path, default=None)
    ap.add_argument("--index", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None,
                    help="Override output path (default: self/profile.md)")
    ap.add_argument("--model", type=str, default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    brain = dropbox_brain_root()
    archives_dir = args.archives_dir or (brain / "self" / "archives")
    facts_dir = args.facts_dir or (brain / "self" / "facts")
    index_path = args.index or (brain / "self" / "archive-index.json")
    out_path = args.out or (brain / "self" / "profile.md")

    print(f"Archives dir: {archives_dir}")
    print(f"Facts dir:    {facts_dir}")
    print(f"Index:        {index_path}")
    print(f"Out:          {out_path}")

    archives = _load_archives(archives_dir)
    if not archives:
        print(f"ERROR: no archives under {archives_dir}", file=sys.stderr)
        print("Run role-archive-synthesize.py first.", file=sys.stderr)
        return 1

    facts = _load_facts(facts_dir)
    if not facts:
        print(f"WARN: no structured facts under {facts_dir} — profile will be archive-only", file=sys.stderr)

    priority_docs = _load_priority_raw_docs(index_path)

    print(f"\nLoaded: {len(archives)} archives, {len(facts)} fact types, "
          f"{len(priority_docs)} priority-flagged raw docs")
    for fact_type, records in facts.items():
        print(f"  facts/{fact_type}: {len(records)} records")
    for path, content in priority_docs:
        name = Path(path).name
        print(f"  priority: {name} ({len(content)} chars)")

    prompt = build_profile_prompt(facts, archives, priority_docs)
    print(f"\nPrompt size: {len(prompt)} chars (~{len(prompt) // 4} tokens)")

    if args.dry_run:
        print(json.dumps({
            "status": "ok",
            "dry_run": True,
            "archives": len(archives),
            "fact_types": list(facts.keys()),
            "priority_docs": len(priority_docs),
            "prompt_chars": len(prompt),
        }))
        return 0

    print(f"\nCalling LLM (model={args.model}, timeout={args.timeout}s)...")
    result = infer(prompt=prompt, model=args.model, timeout=args.timeout, json_mode=False)
    if not result.ok:
        print(f"ERROR: LLM failed: {result.error}", file=sys.stderr)
        return 1

    body_md = result.text or ""
    if not body_md.strip():
        print("ERROR: empty LLM response", file=sys.stderr)
        return 1

    stats = {
        "archives_count": len(archives),
        "fact_types": list(facts.keys()),
        "priority_raw_count": len(priority_docs),
    }
    _write_profile_atomic(out_path, body_md, stats)

    print(f"\nWrote {out_path} ({len(body_md)} chars)")
    print(json.dumps({
        "status": "ok",
        "dry_run": False,
        "out": str(out_path),
        "body_chars": len(body_md),
        "archives": len(archives),
        "fact_types": list(facts.keys()),
        "priority_docs": len(priority_docs),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
