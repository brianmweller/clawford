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
import tempfile
from datetime import datetime, timezone
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

DEFAULT_QUEUE = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/triage-queue.json"))
DEFAULT_LOG = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/auto-compose-log.json"))
CONNECTOR_TOKEN_ENV = "CONNECTOR_BOT_TOKEN"


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


def run_draft_compose(thread_id: str, slug: str, llm_backend: str,
                      no_create_draft: bool = False) -> tuple[int, str, dict]:
    """Run draft-compose, capturing the parsed JSON result via --json-out.
    Returns (exit_code, stdout, parsed_result_dict)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json_out = Path(tf.name)
    try:
        cmd = [
            sys.executable,
            str(_SCRIPTS_DIR / "draft-compose.py"),
            "--person-slug", slug,
            "--gmail-thread-id", thread_id,
            "--llm-backend", llm_backend,
            "--json-out", str(json_out),
        ]
        if no_create_draft:
            cmd.append("--no-create-draft")
        env = os.environ.copy()
        result = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                encoding="utf-8", errors="replace", timeout=600)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = stdout + (f"\n---STDERR---\n{stderr}" if stderr else "")
        parsed = {}
        if json_out.exists() and json_out.stat().st_size > 0:
            try:
                parsed = json.loads(json_out.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                parsed = {}
        return result.returncode, combined, parsed
    finally:
        try:
            json_out.unlink()
        except OSError:
            pass


def _format_telegram(parsed: dict) -> str | None:
    """Return a short Telegram message for a compose result, or None if
    the parsed result is malformed and nothing meaningful to ping."""
    if not parsed:
        return None
    name = parsed.get("from_name") or parsed.get("from_email") or parsed.get("person_slug", "?")
    subject = parsed.get("subject", "(no subject)")
    if parsed.get("reply_needed") is True:
        draft_id = parsed.get("gmail_draft_id") or "?"
        objective = (parsed.get("objective") or "").strip()
        strategy = (parsed.get("strategy") or "").strip()
        return (
            f"📧 Draft ready for {name}\n"
            f"Subject: {subject}\n"
            f"Objective: {objective}\n"
            f"Strategy: {strategy}\n"
            f"Gmail Drafts (id={draft_id})"
        )
    if parsed.get("reply_needed") is False:
        fyi = (parsed.get("no_reply_fyi") or "").strip()
        return (
            f"📬 No reply needed — {name}\n"
            f"Subject: {subject}\n"
            f"{fyi}"
        )
    return None


def _maybe_send_telegram(parsed: dict, dry_run: bool) -> str:
    """Return a status string: 'sent', 'skipped_no_token', 'skipped_dry_run',
    'skipped_malformed', or 'error:<msg>'. Never raises."""
    if dry_run:
        return "skipped_dry_run"
    msg = _format_telegram(parsed)
    if not msg:
        return "skipped_malformed"
    try:
        from agents.shared.telegram_api import resolve_credentials, send_message
    except ImportError as e:
        return f"error:import:{e}"
    try:
        token, chat_id = resolve_credentials(token_env=CONNECTOR_TOKEN_ENV)
    except RuntimeError:
        return "skipped_no_token"
    try:
        send_message(token, chat_id, msg, agent_id="connector",
                     role_summary="draft-review")
        return "sent"
    except Exception as e:   # noqa: BLE001
        return f"error:send:{e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-json", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--log-json", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--llm-backend", default="codex",
                    choices=["codex", "claude-cli", "stdout"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", help="Re-process this thread ID even if logged")
    ap.add_argument("--max", type=int, help="Cap on number of threads to process")
    ap.add_argument("--no-telegram", action="store_true",
                    help="Skip Telegram ping (defaults to on when token env is present)")
    ap.add_argument("--no-create-draft", action="store_true",
                    help="Simulation mode: run compose pipeline but SKIP Gmail draft creation")
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
        rc, output, parsed = run_draft_compose(tid, slug, args.llm_backend,
                                               no_create_draft=args.no_create_draft)

        reply_needed = parsed.get("reply_needed") if parsed else None
        gmail_draft_id = parsed.get("gmail_draft_id") if parsed else None
        verdict = (
            "ERROR" if rc != 0 else
            "DRAFT" if reply_needed is True else
            "FYI" if reply_needed is False else
            "UNKNOWN"
        )

        telegram_status = _maybe_send_telegram(parsed, dry_run=args.no_telegram) if rc == 0 else "skipped_compose_error"

        log[tid] = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "slug": slug,
            "reply_needed": reply_needed,
            "gmail_draft_id": gmail_draft_id,
            "exit_code": rc,
            "subject": item.get("subject", ""),
            "telegram": telegram_status,
        }
        save_log(args.log_json, log)
        print(f"  → {verdict}  gmail_draft={gmail_draft_id}  rc={rc}  telegram={telegram_status}")
        results.append({
            "thread_id": tid, "verdict": verdict,
            "reply_needed": reply_needed, "gmail_draft_id": gmail_draft_id,
            "telegram": telegram_status,
        })

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
