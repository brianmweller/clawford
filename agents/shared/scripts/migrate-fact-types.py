#!/usr/bin/env python3
"""migrate-fact-types.py — one-time pass that classifies legacy narrative
facts into one of four structured types (birthdate / employer / role /
preference) and splices fact_type + value into the month files in place.

Non-destructive:
  - Idempotent: blocks already carrying fact_type are skipped.
  - Dry-run default: emits facts/_migration-proposed.md for operator
    inspection; --commit is required to write.
  - --commit additionally tars the facts dir to
    ~/.clawford/deploy-backups/facts-pre-migration-<ts>.tar.gz BEFORE
    any rewrite, so the operator can roll back a bad run.

Confidence routing (from the approved plan):
  ≥ 0.8  → apply classification directly
  ≥ 0.5  → enqueue on the pending-review queue for operator approval
  < 0.5  → leave fact unchanged (narrative-only)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parent))  # agents/shared

from migrate_fact_types_lib import (  # type: ignore[import-not-found]
    build_classifier_prompt,
    classify_route,
    iter_facts_to_classify,
    parse_classifier_response,
    rewrite_month_file,
)


DEFAULT_FACTS_DIR = Path(os.path.expanduser("~/Dropbox/openclaw-backup/facts"))
DEFAULT_QUEUE = Path(os.path.expanduser(
    "~/.clawford/connector-workspace/cache/pending-review-queue.jsonl"
))
DEFAULT_BACKUP_DIR = Path(os.path.expanduser(
    "~/.clawford/deploy-backups"
))
PROPOSAL_FILENAME = "_migration-proposed.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_infer(prompt: str, timeout: int = 60) -> tuple[bool, str]:
    """Call the shared codex broker. Returns (ok, text)."""
    try:
        from agents.shared.llm import infer  # type: ignore
    except ImportError:
        sys.path.insert(0, str(_SCRIPTS_DIR.parent.parent.parent))
        from agents.shared.llm import infer  # type: ignore
    result = infer(prompt=prompt, json_mode=True, timeout=timeout)
    if not getattr(result, "ok", False):
        return False, ""
    return True, getattr(result, "text", "") or ""


def _format_proposal_block(fact_id: str, subject: str, content: str,
                           fact_type: str, value: dict | None,
                           confidence: float, route: str) -> str:
    return (
        "\n---\n\n"
        f"- **id:** {fact_id}\n"
        f"- **subject:** {subject}\n"
        f"- **content:** {content}\n"
        f"- **proposed_fact_type:** {fact_type or '(none)'}\n"
        f"- **proposed_value:** {json.dumps(value, ensure_ascii=False) if value is not None else '(none)'}\n"
        f"- **migration_confidence:** {confidence:.2f}\n"
        f"- **route:** {route}\n"
    )


def _tarball_backup(facts_dir: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"facts-pre-migration-{ts}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        tar.add(facts_dir, arcname=facts_dir.name)
    return path


def run(
    *,
    facts_dir: Path,
    queue_path: Path,
    commit: bool,
    max_facts: int | None,
    infer_fn=None,
    verbose: bool = False,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
) -> dict:
    infer_fn = infer_fn or _default_infer
    stats = {
        "status": "ok",
        "facts_scanned": 0,
        "facts_already_typed": 0,
        "facts_applied": 0,
        "facts_queued": 0,
        "facts_skipped_low_conf": 0,
        "classifier_errors": 0,
    }
    if not facts_dir.exists():
        stats["status"] = "error"
        stats["error"] = f"facts dir missing: {facts_dir}"
        return stats

    # Collect candidates across every monthly file.
    candidates: list[tuple[Path, str, dict]] = []
    for path in sorted(facts_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        for raw, fields in iter_facts_to_classify(path):
            candidates.append((path, raw, fields))

    # Count facts in general so the operator can see attribution.
    for path in sorted(facts_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        total_blocks = 0
        for raw in text.split("\n---\n"):
            if raw.strip() and "- **id:**" in raw:
                total_blocks += 1
        stats["facts_scanned"] += total_blocks
    stats["facts_already_typed"] = stats["facts_scanned"] - len(candidates)

    if max_facts is not None:
        candidates = candidates[:max_facts]

    # Classifications per-month-file → applied dict.
    applied_by_file: dict[Path, dict[str, tuple[str, dict]]] = {}
    proposal_blocks: list[str] = []
    queue_entries: list[dict] = []

    for path, _raw, fields in candidates:
        fact_id = fields["id"]
        subject = fields.get("subject", "")
        content = fields.get("content", "")
        if not subject or not content:
            continue

        prompt = build_classifier_prompt(
            content=content, subject=subject, category=fields.get("category", ""),
        )
        try:
            ok, text = infer_fn(prompt)
        except Exception:
            stats["classifier_errors"] += 1
            if verbose:
                traceback.print_exc(file=sys.stderr)
            continue
        if not ok:
            stats["classifier_errors"] += 1
            continue

        parsed = parse_classifier_response(text)
        ft = parsed["fact_type"]
        val = parsed["value"]
        conf = parsed["migration_confidence"]
        route = classify_route(conf)
        if not ft:
            route = "skip"  # unrecognized type → skip regardless of confidence

        proposal_blocks.append(
            _format_proposal_block(fact_id, subject, content, ft, val, conf, route)
        )

        if route == "apply" and ft:
            applied_by_file.setdefault(path, {})[fact_id] = (ft, val)
            stats["facts_applied"] += 1
        elif route == "queue" and ft:
            queue_entries.append({
                "id": f"migration-{fact_id}",
                "source": "migration",
                "fact": {
                    "id": fact_id,
                    "subject": subject,
                    "content": content,
                    "proposed_fact_type": ft,
                    "proposed_value": val,
                    "migration_confidence": conf,
                },
                "question": (
                    f"Classify as {ft}? "
                    f"[Apply] keeps the structured value; [Skip] leaves narrative-only."
                ),
                "options": ["approve", "reject", "skip"],
                "created_at": _now_iso(),
            })
            stats["facts_queued"] += 1
        else:
            stats["facts_skipped_low_conf"] += 1

    # Emit the proposal document regardless of mode so operators can
    # inspect what a --commit would do.
    header = (
        f"# Proposed migration — {_now_iso()}\n\n"
        f"- scanned: {stats['facts_scanned']}\n"
        f"- already_typed: {stats['facts_already_typed']}\n"
        f"- candidates: {len(candidates)}\n"
        f"- apply: {stats['facts_applied']}  queue: {stats['facts_queued']}  skip: {stats['facts_skipped_low_conf']}\n"
        f"- classifier_errors: {stats['classifier_errors']}\n"
    )
    proposal_path = facts_dir / PROPOSAL_FILENAME
    proposal_path.write_text(header + "".join(proposal_blocks), encoding="utf-8")

    if not commit:
        stats["mode"] = "dry_run"
        stats["proposal_path"] = str(proposal_path)
        return stats

    # Commit: tarball + atomic per-file rewrite + enqueue.
    backup = _tarball_backup(facts_dir, backup_dir)
    stats["backup"] = str(backup)

    total_applied = 0
    for path, applications in applied_by_file.items():
        total_applied += rewrite_month_file(path, applications)
    stats["facts_applied_on_disk"] = total_applied

    # Enqueue medium-confidence rows.
    if queue_entries:
        try:
            from agents.shared import pending_queue  # type: ignore
        except ImportError:
            sys.path.insert(0, str(_SCRIPTS_DIR.parent))
            import pending_queue  # type: ignore
        for e in queue_entries:
            pending_queue.append(queue_path, e)
    stats["mode"] = "commit"
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-dir", type=Path, default=DEFAULT_FACTS_DIR)
    ap.add_argument("--queue-path", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    ap.add_argument("--commit", action="store_true",
                    help="Actually write changes. Default is dry-run (writes proposal only).")
    ap.add_argument("--max", type=int, default=None,
                    help="Cap on facts to classify per run. Useful for a staged roll-out.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    try:
        result = run(
            facts_dir=args.facts_dir,
            queue_path=args.queue_path,
            commit=args.commit,
            max_facts=args.max,
            verbose=args.verbose,
            backup_dir=args.backup_dir,
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
    sys.exit(main())
