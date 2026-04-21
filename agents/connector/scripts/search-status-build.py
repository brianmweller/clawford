#!/usr/bin/env python3
"""search-status-build.py — mine Gmail + Workflowy for the operator's active
executive search pipeline; write self/search-status.md.

Inputs:
  - Gmail: threads in last N days from recruiter-platform domains
    (RECRUITER_DOMAINS from recruiter_detector_lib).
  - Workflowy: meeting records whose titles match interview-prep /
    phone-screen / onsite / final-round patterns.

Synthesis:
  - Single LLM call (gpt-5.4-mini) given combined evidence.
  - Output: {active_searches: [...], recently_concluded: [...]}.

Outputs:
  - self/search-status.md          (human-readable table)
  - self/facts/active_search_stages.json  (programmatic)

Consumers:
  - profile.md synthesis: reads search-status.md as a priority doc so
    the narrative profile reflects current pipeline urgency.
  - Huckle cold-inbound compose: SELF CONTEXT block includes 'in late
    stages with X, Y, Z' to calibrate draft urgency.

Usage:
  python3 search-status-build.py --dry-run        # show evidence only
  python3 search-status-build.py                  # real run
  python3 search-status-build.py --window 180     # 180-day window
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root        # noqa: E402
from agents.shared.llm import infer                       # noqa: E402
from recruiter_detector_lib import RECRUITER_DOMAINS      # noqa: E402
from search_status_lib import (                           # noqa: E402
    build_status_prompt,
    extract_gmail_evidence,
    extract_workflowy_evidence,
    format_status_md,
    parse_status_response,
)
from workflowy_walker import get_nodes_export, iter_meeting_records  # noqa: E402


DEFAULT_TOKEN = Path(os.path.expanduser("~/.clawford/connector-workspace/token.json"))
DEFAULT_CREDS = Path(os.path.expanduser("~/.clawford/connector-workspace/credentials.json"))
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT_S = 240


def _build_gmail_service(token: Path, creds: Path):
    from googleapiclient.discovery import build
    from agents.shared.google_oauth import get_credentials
    scopes = [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    gcreds = get_credentials(str(creds), str(token), scopes)
    return build("gmail", "v1", credentials=gcreds)


def _build_recruiter_thread_query(domains: list[str], window_days: int) -> str:
    from_clause = " OR ".join(f"from:{d}" for d in domains)
    return f"newer_than:{window_days}d ({from_clause})"


def _fetch_recruiter_threads(service, *, window_days: int, max_threads: int = 100) -> list[dict]:
    """Fetch Gmail threads from recruiter-platform senders in the last
    window_days. Returns threads in the shape extract_gmail_evidence
    expects: [{id, messages: [{date, sender, subject, snippet}, ...]}].
    """
    # Chunk domains to keep query length sane (Gmail has query length limits)
    domain_chunks = []
    chunk: list[str] = []
    for d in sorted(RECRUITER_DOMAINS):
        chunk.append(d)
        if len(chunk) >= 15:
            domain_chunks.append(chunk)
            chunk = []
    if chunk:
        domain_chunks.append(chunk)

    thread_ids: set[str] = set()
    for chunk in domain_chunks:
        q = _build_recruiter_thread_query(chunk, window_days)
        resp = service.users().threads().list(userId="me", q=q, maxResults=max_threads).execute()
        for t in resp.get("threads", []):
            thread_ids.add(t["id"])

    threads: list[dict] = []
    for tid in thread_ids:
        try:
            t = service.users().threads().get(userId="me", id=tid, format="metadata").execute()
        except Exception:  # noqa: BLE001 — Gmail occasionally 500s
            continue
        messages_out = []
        for m in t.get("messages", []):
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            messages_out.append({
                "date": headers.get("date", ""),
                "sender": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "snippet": m.get("snippet", ""),
            })
        threads.append({"id": tid, "messages": messages_out})
    return threads


def _fetch_workflowy_interview_records(window_days: int) -> list[dict]:
    """Pull the flat Workflowy export, iterate meetings, keep those in
    the time window. Returns records in the shape extract_workflowy_
    evidence expects."""
    nodes = get_nodes_export()
    if not nodes:
        return []
    cutoff = datetime.now(timezone.utc).date().toordinal() - window_days
    out: list[dict] = []
    for rec in iter_meeting_records(nodes):
        date_str = (rec.get("parent_date") or "")[:10]
        if not date_str:
            continue
        try:
            rec_date = datetime.fromisoformat(date_str).date()
        except ValueError:
            continue
        if rec_date.toordinal() < cutoff:
            continue
        out.append({
            "path": rec["path"],
            "chronological_date": date_str,
            "summary": (rec.get("text_excerpt") or "")[:240],
            "text_excerpt": rec.get("text_excerpt", ""),
        })
    return out


def _write_outputs(data: dict, *, md_path: Path, json_path: Path, generated_at: str) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md = format_status_md(data, generated_at=generated_at)
    tmp_md = md_path.with_suffix(md_path.suffix + ".tmp")
    tmp_md.write_text(md, encoding="utf-8")
    tmp_md.replace(md_path)

    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": generated_at, **data}
    tmp_json = json_path.with_suffix(json_path.suffix + ".tmp")
    tmp_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_json.replace(json_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=90,
                    help="Lookback window in days (default: 90)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch evidence, print summary, skip LLM + writes")
    ap.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    ap.add_argument("--creds", type=Path, default=DEFAULT_CREDS)
    ap.add_argument("--model", type=str, default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--out-md", type=Path, default=None)
    ap.add_argument("--out-json", type=Path, default=None)
    ap.add_argument("--skip-gmail", action="store_true",
                    help="Skip Gmail fetch (Workflowy-only run)")
    ap.add_argument("--skip-workflowy", action="store_true",
                    help="Skip Workflowy fetch (Gmail-only run)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    brain = dropbox_brain_root()
    md_path = args.out_md or (brain / "self" / "search-status.md")
    json_path = args.out_json or (brain / "self" / "facts" / "active_search_stages.json")

    print(f"Window:   {args.window} days")
    print(f"Out md:   {md_path}")
    print(f"Out json: {json_path}")

    # Gmail fetch
    gmail_evidence: list[dict] = []
    if not args.skip_gmail:
        print("\nFetching Gmail threads from recruiter-platform senders...")
        service = _build_gmail_service(args.token, args.creds)
        threads = _fetch_recruiter_threads(service, window_days=args.window)
        gmail_evidence = extract_gmail_evidence(threads)
        print(f"  {len(threads)} threads → {len(gmail_evidence)} evidence records")
        for e in gmail_evidence[:6]:
            subj = e["subject"][:70]
            print(f"    [{e['last_date'][:10] or '?'}] {e['message_count']} msgs: {subj}")
        if len(gmail_evidence) > 6:
            print(f"    ... and {len(gmail_evidence) - 6} more")

    # Workflowy fetch
    wf_evidence: list[dict] = []
    if not args.skip_workflowy:
        print("\nFetching Workflowy interview-prep records...")
        wf_records = _fetch_workflowy_interview_records(args.window)
        wf_evidence = extract_workflowy_evidence(wf_records)
        print(f"  {len(wf_records)} meeting records in window → "
              f"{len(wf_evidence)} interview-related")
        for e in wf_evidence[:6]:
            print(f"    [{e['chronological_date']}] {e['summary'][:100]}")
        if len(wf_evidence) > 6:
            print(f"    ... and {len(wf_evidence) - 6} more")

    if args.dry_run:
        print()
        print(json.dumps({
            "status": "ok",
            "dry_run": True,
            "gmail_evidence_count": len(gmail_evidence),
            "workflowy_evidence_count": len(wf_evidence),
        }))
        return 0

    if not gmail_evidence and not wf_evidence:
        print("\nWARN: no evidence from either source; writing empty pipeline")
        data = {"active_searches": [], "recently_concluded": []}
    else:
        prompt = build_status_prompt(gmail_evidence, wf_evidence)
        print(f"\nCalling LLM ({args.model}, prompt {len(prompt)} chars)...")
        result = infer(prompt=prompt, model=args.model, timeout=args.timeout, json_mode=True)
        if not result.ok:
            print(f"ERROR: LLM failed: {result.error}", file=sys.stderr)
            return 1
        data = parse_status_response(result.text or "")

    generated_at = datetime.now(timezone.utc).isoformat()
    _write_outputs(data, md_path=md_path, json_path=json_path, generated_at=generated_at)

    active = data.get("active_searches") or []
    concluded = data.get("recently_concluded") or []
    print(f"\nActive pipeline: {len(active)}  |  Recently concluded: {len(concluded)}")
    for e in active:
        print(f"  [{e['stage']:20s}] {e['company']:20s} — {e.get('last_signal_date', '')} — "
              f"{(e.get('notes') or '')[:80]}")
    print(f"\nWrote {md_path}")
    print(f"Wrote {json_path}")

    print(json.dumps({
        "status": "ok",
        "dry_run": False,
        "active_count": len(active),
        "concluded_count": len(concluded),
        "out_md": str(md_path),
        "out_json": str(json_path),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
