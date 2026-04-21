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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


_SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

DEFAULT_QUEUE = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/triage-queue.json"))
DEFAULT_LOG = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/auto-compose-log.json"))
DEFAULT_SCHEDULING_RULES = Path(os.path.expanduser("~/.clawford/connector-workspace/scheduling.rules.json"))
CONNECTOR_TOKEN_ENV = "CONNECTOR_BOT_TOKEN"


def default_scheduling_rules_path(path: Path = DEFAULT_SCHEDULING_RULES) -> Path | None:
    """Return the scheduling rules path if it exists, else None.
    Drafts compose without a slots block when the file is missing."""
    return path if path.exists() else None


def load_timezone_from_rules(path: Path) -> str:
    """Read the 'timezone' field from a scheduling rules JSON, defaulting
    to America/Los_Angeles if absent or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tz = data.get("timezone")
        if isinstance(tz, str) and tz:
            return tz
    except (OSError, json.JSONDecodeError):
        pass
    return "America/Los_Angeles"


def compute_default_search_window(
    timezone_str: str,
    days_ahead: int = 14,
    now: datetime | None = None,
) -> str:
    """Build a 'ISO-start/ISO-end/tz' window starting tomorrow 09:00 through
    now+days_ahead 18:00 in the given tz. `now` is injectable for tests."""
    tz = ZoneInfo(timezone_str)
    current = now.replace(tzinfo=tz) if now and now.tzinfo is None else (now or datetime.now(tz))
    start = (current + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    end = (current + timedelta(days=days_ahead)).replace(hour=18, minute=0, second=0, microsecond=0)
    return f"{start.strftime('%Y-%m-%dT%H:%M')}/{end.strftime('%Y-%m-%dT%H:%M')}/{timezone_str}"


def parse_search_window(window: str) -> tuple[datetime, datetime]:
    """Inverse of compute_default_search_window — return tz-aware start/end
    datetimes from a 'ISO-start/ISO-end/tz' window."""
    start_s, end_s, tz_s = window.split("/", 2)
    tz = ZoneInfo(tz_s)
    return (
        datetime.fromisoformat(start_s).replace(tzinfo=tz),
        datetime.fromisoformat(end_s).replace(tzinfo=tz),
    )


def materialize_busy_blocks(blocks: list[dict], dest_dir: Path) -> Path | None:
    """Write a list of {"start","end"} busy blocks to a JSON file in
    dest_dir and return the path. Returns None when blocks is empty —
    draft-compose.py simply omits busy filtering, which is cheaper than
    passing an empty-list tempfile."""
    if not blocks:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "busy-blocks.json"
    path.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    return path


def build_draft_compose_cmd(
    thread_id: str,
    slug: str | None,
    llm_backend: str,
    json_out: Path,
    *,
    no_create_draft: bool = False,
    scheduling_rules: Path | None = None,
    search_window: str | None = None,
    busy_blocks: Path | None = None,
    cold_inbound: bool = False,
) -> list[str]:
    """Assemble the draft-compose.py subprocess command. Pure: no I/O.

    cold_inbound=True means --cold-inbound gets passed instead of
    --person-slug; draft-compose loads the operator's self-profile and injects
    FIT CHECK + SELF CONTEXT for unknown-recruiter drafting.

    When scheduling_rules and search_window are both provided, the draft
    pipeline computes OPEN SLOTS from them (optionally filtered by
    busy_blocks). When either is omitted, the draft pipeline falls back
    to freehand scheduling — the LLM guesses with no grounding.
    """
    cmd = [
        sys.executable,
        str(_SCRIPTS_DIR / "draft-compose.py"),
        "--gmail-thread-id", thread_id,
        "--llm-backend", llm_backend,
        "--json-out", str(json_out),
    ]
    if cold_inbound:
        cmd.append("--cold-inbound")
    else:
        if not slug:
            raise ValueError("slug is required when cold_inbound=False")
        cmd.extend(["--person-slug", slug])
    if no_create_draft:
        cmd.append("--no-create-draft")
    if scheduling_rules and search_window:
        cmd.extend(["--scheduling-rules", str(scheduling_rules)])
        cmd.extend(["--search-window", search_window])
    if busy_blocks:
        cmd.extend(["--busy-blocks", str(busy_blocks)])
    return cmd


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


def run_draft_compose(
    thread_id: str,
    slug: str | None,
    llm_backend: str,
    no_create_draft: bool = False,
    scheduling_rules: Path | None = None,
    search_window: str | None = None,
    busy_blocks: Path | None = None,
    cold_inbound: bool = False,
) -> tuple[int, str, dict]:
    """Run draft-compose, capturing the parsed JSON result via --json-out.
    Returns (exit_code, stdout, parsed_result_dict)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json_out = Path(tf.name)
    try:
        cmd = build_draft_compose_cmd(
            thread_id=thread_id,
            slug=slug,
            llm_backend=llm_backend,
            json_out=json_out,
            no_create_draft=no_create_draft,
            scheduling_rules=scheduling_rules,
            search_window=search_window,
            busy_blocks=busy_blocks,
            cold_inbound=cold_inbound,
        )
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
    the parsed result is malformed and nothing meaningful to ping.

    For cold-recruiter drafts (fit_assessment present), the ping leads
    with the fit tier so the operator can calibrate at a glance before opening
    Gmail."""
    if not parsed:
        return None
    name = parsed.get("from_name") or parsed.get("from_email") or parsed.get("person_slug", "?")
    subject = parsed.get("subject", "(no subject)")
    fit = parsed.get("fit_assessment") or {}
    fit_tier = fit.get("tier", "")
    fit_rationale = (fit.get("rationale") or "").strip()
    matched_target = (fit.get("matched_target") or "").strip()

    if parsed.get("reply_needed") is True:
        draft_id = parsed.get("gmail_draft_id") or "?"
        objective = (parsed.get("objective") or "").strip()
        strategy = (parsed.get("strategy") or "").strip()
        header = f"📧 Draft ready for {name}"
        fit_line = ""
        if fit_tier:
            tier_emoji = {"A": "🟢", "B": "🟡", "C": "🔴",
                          "not_a_target": "🔴", "unclear": "⚪"}.get(fit_tier, "⚪")
            target_suffix = f" (matches {matched_target})" if matched_target else ""
            fit_line = f"\nFit: {tier_emoji} {fit_tier}-tier{target_suffix} — {fit_rationale}"
        # Surface the second-pass redundancy prune count so the operator can
        # eyeball whether the pruner is cutting too aggressively. Only
        # appears when sentences were actually removed.
        redundancy_line = ""
        red = parsed.get("redundancy") or {}
        removed_count = int(red.get("removed_count") or 0)
        if removed_count:
            sources: set[str] = set()
            for r in red.get("removed") or []:
                src = str((r or {}).get("source") or "")
                if src == "inbound":
                    sources.add("inbound")
                elif src.startswith("fact:"):
                    sources.add("known")
            src_label = "inbound + known" if {"inbound", "known"}.issubset(sources) \
                else ("inbound" if "inbound" in sources else "known" if "known" in sources else "redundant")
            redundancy_line = f"\nTrimmed: {removed_count} sentence{'s' if removed_count != 1 else ''} ({src_label})"
        return (
            f"{header}"
            f"{fit_line}\n"
            f"Subject: {subject}\n"
            f"Objective: {objective}\n"
            f"Strategy: {strategy}"
            f"{redundancy_line}\n"
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


def _build_recruiter_markup(parsed: dict) -> dict | None:
    """Build an inline keyboard for cold-recruiter FYI messages:
      [✅ Promote] [🚫 Not a fit]
    Only attaches to drafts that carry a `fit_assessment` (cold-recruiter
    runs) AND a thread_id we can encode into callback_data. For
    `not_a_target` tier we skip the buttons — the operator can still
    `/promote <thread_id>` manually but the happy path is dismiss.
    """
    if not parsed:
        return None
    fit = parsed.get("fit_assessment") or {}
    tier = fit.get("tier", "")
    if not tier:
        return None
    if tier == "not_a_target":
        return None
    thread_id = parsed.get("gmail_thread_id") or parsed.get("thread_id") or ""
    if not thread_id:
        return None
    return {
        "inline_keyboard": [[
            {"text": "✅ Promote", "callback_data": f"recruiter:promote:{thread_id}"},
            {"text": "\U0001f6ab Not a fit", "callback_data": f"recruiter:reject:{thread_id}"},
        ]],
    }


def _maybe_send_telegram(parsed: dict, dry_run: bool) -> str:
    """Return a status string: 'sent', 'skipped_no_token', 'skipped_dry_run',
    'skipped_malformed', or 'error:<msg>'. Never raises."""
    if dry_run:
        return "skipped_dry_run"
    msg = _format_telegram(parsed)
    if not msg:
        return "skipped_malformed"
    markup = _build_recruiter_markup(parsed)
    try:
        from agents.shared.telegram_api import resolve_credentials, send_message
    except ImportError as e:
        return f"error:import:{e}"
    try:
        token, chat_id = resolve_credentials(token_env=CONNECTOR_TOKEN_ENV)
    except RuntimeError:
        return "skipped_no_token"
    try:
        send_message(
            token, chat_id, msg, agent_id="connector",
            role_summary="draft-review", reply_markup=markup,
        )
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
    ap.add_argument("--max", type=int, default=5,
                    help="Cap on threads processed per run. Defaults to 5 as a safety "
                         "rail for cron invocations. Pass a larger number for manual bulk runs.")
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

    scheduling_rules = default_scheduling_rules_path()
    search_window = None
    busy_blocks_path = None
    if scheduling_rules:
        tz = load_timezone_from_rules(scheduling_rules)
        search_window = compute_default_search_window(tz, days_ahead=14)
        print(f"Scheduling rules: {scheduling_rules}")
        print(f"Search window: {search_window}")

        # Fetch calendar busy blocks so LLM-proposed times don't collide
        # with existing meetings. Silent-fails to empty on API error —
        # freehand scheduling is the fallback, not a crash.
        try:
            from agents.shared.gmail_api import build_gmail_service
            from agents.shared.gcal_freebusy import query_busy

            token_path = str(Path(os.path.expanduser(
                "~/.clawford/connector-workspace/token.json")))
            creds_path = str(Path(os.path.expanduser(
                "~/.clawford/connector-workspace/credentials.json")))
            service = build_gmail_service(
                token_path, creds_path,
                scopes=[
                    "https://www.googleapis.com/auth/calendar.readonly",
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.compose",
                ],
            )
            s, e = parse_search_window(search_window)
            blocks = query_busy(service, s, e)
            cache_dir = Path(os.path.expanduser(
                "~/.clawford/connector-workspace/cache"))
            busy_blocks_path = materialize_busy_blocks(blocks, cache_dir)
            print(f"Busy blocks: {len(blocks)} (path={busy_blocks_path})")
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: busy-block fetch failed ({exc}); using freehand only")
        print()

    results = []
    for item in to_process:
        tid = item["thread_id"]
        is_cold = item.get("status") == "queued_cold_recruiter"
        slug = item.get("slug") if not is_cold else None
        label = f"COLD-RECRUITER" if is_cold else (slug or "?")
        print(f"COMPOSING [{tid}] {label} ...")
        rc, output, parsed = run_draft_compose(
            tid, slug, args.llm_backend,
            no_create_draft=args.no_create_draft,
            scheduling_rules=scheduling_rules,
            search_window=search_window,
            busy_blocks=busy_blocks_path,
            cold_inbound=is_cold,
        )

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
            "cold_inbound": is_cold,
            "fit_tier": (parsed.get("fit_assessment") or {}).get("tier", "") if parsed else "",
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
