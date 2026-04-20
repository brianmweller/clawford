#!/usr/bin/env python3
"""auto-compose.py — for each queued thread from inbox-triage, invoke
draft-compose and track state so we don't re-draft.

Reads cache/triage-queue.json (produced by inbox-triage.py), and for each
entry not already in cache/auto-compose-log.json, runs draft-compose.py
as a subprocess with --gmail-thread-id and the slug from triage. The
draft-compose CLI handles Gmail fetch + LLM + draft creation; this script
is the orchestrator.

State file cache/auto-compose-log.json shape:
  {
    "<thread_id>": {
      "at": "2026-04-19T23:45:00Z",
      "slug": "ravi-rivera",
      "reply_needed": true | false,
      "gmail_draft_id": "<draft id>" | null,
      "exit_code": 0
    },
    ...
  }

Usage:
  python3 auto-compose.py --dry-run          # list what would process
  python3 auto-compose.py                    # actually compose
  python3 auto-compose.py --force <tid>      # re-process a specific thread

No Telegram integration yet — drafts land in Gmail; operator checks.
Telegram ping is a follow-up when cron'd on the VPS.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_QUEUE = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/triage-queue.json"))
DEFAULT_LOG = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/auto-compose-log.json"))


def load_log(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_log(path: Path, log: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def run_draft_compose(thread_id: str, slug: str, llm_backend: str) -> tuple[int, str]:
    cmd = [
        sys.executable,
        str(_SCRIPTS_DIR / "draft-compose.py"),
        "--person-slug", slug,
        "--gmail-thread-id", thread_id,
        "--llm-backend", llm_backend,
    ]
    env = os.environ.copy()
    # Ensure the brain root env var is set for the child process
    if "CLAWFORD_BRAIN_DROPBOX_ROOT" not in env:
        # Best-effort default; child will error if mismatched
        pass
    result = subprocess.run(cmd, capture_output=True, text=True, env=env,
                            encoding="utf-8", errors="replace", timeout=600)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = stdout + (f"\n---STDERR---\n{stderr}" if stderr else "")
    return result.returncode, combined


def parse_compose_output(stdout: str) -> dict:
    """Scrape the already-printed VERDICT and draft-id lines to summarize."""
    reply_needed = None
    gmail_draft_id = None
    for line in stdout.splitlines():
        if line.startswith("VERDICT: reply_needed=TRUE"):
            reply_needed = True
        elif line.startswith("VERDICT: reply_needed=FALSE"):
            reply_needed = False
        elif line.startswith("GMAIL DRAFT CREATED:"):
            # "GMAIL DRAFT CREATED: id=r-5525 threadId=..."
            for tok in line.split():
                if tok.startswith("id="):
                    gmail_draft_id = tok.split("=", 1)[1]
                    break
    return {"reply_needed": reply_needed, "gmail_draft_id": gmail_draft_id}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-json", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--log-json", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--llm-backend", default="codex",
                    choices=["codex", "claude-cli", "stdout"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", help="Re-process this thread ID even if logged")
    ap.add_argument("--max", type=int, help="Cap on number of threads to process")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    if not args.queue_json.exists():
        print(f"ERROR: queue file not found at {args.queue_json}", file=sys.stderr)
        print("Run inbox-triage.py first.", file=sys.stderr)
        return 1

    queue = json.loads(args.queue_json.read_text(encoding="utf-8"))
    log = load_log(args.log_json)

    print(f"Queue: {args.queue_json}")
    print(f"Log: {args.log_json}")
    print(f"Queued threads: {len(queue.get('queued', []))}")
    print(f"Previously processed: {len(log)}")
    print()

    to_process = []
    for item in queue.get("queued", []):
        tid = item["thread_id"]
        if args.force == tid:
            to_process.append(item)
        elif tid in log:
            continue
        else:
            to_process.append(item)
    if args.max:
        to_process = to_process[: args.max]

    print(f"To process this run: {len(to_process)}")
    print()

    if not to_process:
        print("Nothing to do.")
        print(json.dumps({"status": "ok", "processed": 0, "skipped_logged": len(log)}))
        return 0

    if args.dry_run:
        print("DRY RUN — would process:")
        for item in to_process:
            print(f"  [{item['thread_id']}] {item['slug']:25s} {item.get('subject','')[:60]}")
        print()
        print(json.dumps({"status": "ok", "dry_run": True, "would_process": len(to_process)}))
        return 0

    results = []
    for item in to_process:
        tid = item["thread_id"]
        slug = item["slug"]
        print(f"COMPOSING [{tid}] {slug} ...")
        rc, output = run_draft_compose(tid, slug, args.llm_backend)
        summary = parse_compose_output(output)
        log[tid] = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "slug": slug,
            "reply_needed": summary["reply_needed"],
            "gmail_draft_id": summary["gmail_draft_id"],
            "exit_code": rc,
            "subject": item.get("subject", ""),
        }
        save_log(args.log_json, log)
        verdict = (
            "ERROR" if rc != 0 else
            "DRAFT" if summary["reply_needed"] else
            "FYI" if summary["reply_needed"] is False else
            "UNKNOWN"
        )
        print(f"  → {verdict}  gmail_draft={summary['gmail_draft_id']}  rc={rc}")
        results.append({"thread_id": tid, "verdict": verdict, **summary})

    print()
    print("=" * 64)
    print(f"Processed {len(results)}:")
    for r in results:
        print(f"  {r['verdict']:6s} {r['thread_id']}  draft={r['gmail_draft_id']}")
    print()
    print(json.dumps({
        "status": "ok",
        "processed": len(results),
        "drafts_created": sum(1 for r in results if r["verdict"] == "DRAFT"),
        "fyis": sum(1 for r in results if r["verdict"] == "FYI"),
        "errors": sum(1 for r in results if r["verdict"] == "ERROR"),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
