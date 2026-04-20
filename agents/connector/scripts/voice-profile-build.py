#!/usr/bin/env python3
"""voice-profile-build.py — one-time pass to learn the operator's writing voice
per circle from his sent Gmail.

For each circle (family-inner, friends-close, professional-inner, etc.):
  1. Get email→circle map from people/*.md.
  2. Query Gmail for sent messages to people in that circle (last 1y).
  3. Random-sample up to N messages, fetch full bodies.
  4. Call an LLM with the samples + an extraction prompt.
  5. Write cache/voice-profiles/<circle>.json (also a human-readable
     review.md so the operator can sanity-check before the first compose
     run uses it).

The profile is loaded at compose time by voice.py to fill in voice
details that aren't derivable from the per-thread history anchor
(especially for new contacts or cold outreach).

Uses the existing gmail.readonly scope. LLM backend defaults to claude-cli
(voice-building is quality-sensitive and this is a one-off, not VPS cron);
pass --llm-backend codex to use the ChatGPT subscription instead.

Usage:
  python3 voice-profile-build.py --circle family-inner --dry-run
  python3 voice-profile-build.py --circle family-inner
  python3 voice-profile-build.py --all
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path


for _p in Path(__file__).resolve().parents:
    if (_p / "agents" / "shared").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root                  # noqa: E402
from agents.shared.gmail_api import extract_plain_body              # noqa: E402
from voice_profile_lib import (                                     # noqa: E402
    bucket_people_by_circle,
    build_profile_extraction_prompt,
    exclusive_emails_for_circle,
    parse_profile_response,
)


EXCLUSIVE_FALLBACK_THRESHOLD = 5   # if circle-exclusive emails < this, fall back to all
MIN_SAMPLES_FOR_PROFILE = 15       # below this, voice inference is too noisy — skip


DEFAULT_CACHE = Path(os.path.expanduser("~/.clawford/connector-workspace/cache/voice-profiles"))
PERSON_CACHE_SUBDIR = "person"
MIN_SAMPLES_FOR_PERSON_PROFILE = 10  # lower than circle's 15 — per-person data is sparser
DEFAULT_TOKEN = Path(os.path.expanduser("~/.clawford/connector-workspace/token.json"))
DEFAULT_CREDS = Path(os.path.expanduser("~/.clawford/connector-workspace/credentials.json"))


def chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _lookup_person_email(people_dir: Path, slug: str) -> str | None:
    """Return the email address stored in people/<slug>.md, or None."""
    import re
    path = people_dir / f"{slug}.md"
    if not path.exists():
        return None
    field_re = re.compile(r"^\s*-\s*\*\*email(?::\*\*|\*\*:)\s*(.+?)\s*$")
    for line in path.read_text(encoding="utf-8").splitlines():
        m = field_re.match(line)
        if m:
            val = m.group(1).strip().lower()
            if val and val not in {"—", "-"} and "@" in val:
                return val
    return None


def fetch_sent_messages_for_circle(
    service,
    emails: list[str],
    *,
    window_days: int = 365,
    chunk_size: int = 15,
    total_cap: int = 200,
) -> list[str]:
    """Return message IDs of sent mail addressed to any email in the
    circle within the last window_days."""
    collected: list[str] = []
    for chunk in chunks(emails, chunk_size):
        q = f"in:sent newer_than:{window_days}d " + " OR ".join(f"to:{e}" for e in chunk)
        resp = service.users().messages().list(
            userId="me", q=q, maxResults=100,
        ).execute()
        for m in resp.get("messages", []):
            collected.append(m["id"])
            if len(collected) >= total_cap:
                return collected
    return collected


def fetch_message_sample(service, message_id: str) -> dict | None:
    """Fetch full message + extract {to, date, body} — returns None on error."""
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full",
        ).execute()
    except Exception:
        return None

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body = extract_plain_body(msg)
    if not body.strip():
        return None
    return {
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "body": body,
    }


def call_codex(prompt: str, timeout: int = 240) -> str:
    from agents.shared.llm import infer
    result = infer(prompt=prompt, json_mode=True, timeout=timeout)
    if not result.ok:
        return json.dumps({"error": f"codex infer failed: {result.error}"})
    return result.text or ""


def build_profile_for_circle(
    circle: str,
    emails: list[str],
    service,
    *,
    sample_size: int,
    llm_backend: str,
    verbose: bool = False,
    min_samples: int | None = None,
) -> dict:
    """Returns {circle, samples_used, profile, extracted_at} or
    {error, raw} on failure."""
    if not emails:
        return {"error": f"no emails in circle {circle}"}
    print(f"[{circle}] fetching sent-mail IDs for {len(emails)} recipients...")
    ids = fetch_sent_messages_for_circle(service, emails)
    print(f"[{circle}] {len(ids)} candidate messages")
    if not ids:
        return {"error": f"no sent messages found to {circle}"}

    random.seed(1)   # reproducible sampling for iterative runs
    sampled_ids = random.sample(ids, min(sample_size, len(ids)))
    samples: list[dict] = []
    for mid in sampled_ids:
        s = fetch_message_sample(service, mid)
        if s:
            # Guard prompt size: cap each body to 1200 chars
            s["body"] = s["body"][:1200]
            samples.append(s)
    print(f"[{circle}] fetched {len(samples)} sample bodies")

    if not samples:
        return {"error": f"no usable samples for {circle}"}

    threshold = min_samples if min_samples is not None else MIN_SAMPLES_FOR_PROFILE
    if len(samples) < threshold:
        return {
            "error": f"only {len(samples)} samples (need >= {threshold}); "
                     f"voice inference from too few messages is unreliable — skipping "
                     f"profile. draft-compose will fall through to register calibration.",
            "circle": circle,
        }

    prompt = build_profile_extraction_prompt(circle, samples)
    if verbose:
        print(f"[{circle}] prompt size: {len(prompt)} chars")

    print(f"[{circle}] calling LLM ({llm_backend})...")
    llm_text = call_codex(prompt)
    profile = parse_profile_response(llm_text)
    if "error" in profile:
        return {"error": profile["error"], "raw": llm_text[:2000], "circle": circle}

    return {
        "circle": circle,
        "samples_used": len(samples),
        "extracted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "llm_backend": llm_backend,
        "profile": profile,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--circle", help="Build profile for one circle (e.g. family-inner)")
    ap.add_argument("--all", action="store_true", help="Build profiles for all circles")
    ap.add_argument("--person", help="Build per-person override profile (e.g. ravi-rivera)")
    ap.add_argument("--sample-size", type=int, default=30)
    ap.add_argument("--people-dir", type=Path)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    ap.add_argument("--creds", type=Path, default=DEFAULT_CREDS)
    ap.add_argument("--llm-backend", default="codex",
                    choices=["codex"])
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be built but skip LLM and writes")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass

    modes = [bool(args.circle), bool(args.all), bool(args.person)]
    if sum(modes) != 1:
        print("ERROR: pass exactly one of --circle <name>, --all, or --person <slug>",
              file=sys.stderr)
        return 1

    people_dir = args.people_dir or (dropbox_brain_root() / "people")

    # Per-person profile mode: different flow, skip circle discovery
    if args.person:
        person_email = _lookup_person_email(people_dir, args.person)
        if not person_email:
            print(f"ERROR: person slug {args.person!r} not found or has no email",
                  file=sys.stderr)
            return 1
        print(f"Building per-person profile: {args.person} <{person_email}>")
        if args.dry_run:
            print(f"DRY RUN — would query sent mail to {person_email}, sample up to {args.sample_size}")
            return 0
        from googleapiclient.discovery import build as _build
        from agents.shared.google_oauth import get_credentials
        scopes = [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ]
        creds = get_credentials(str(args.creds), str(args.token), scopes)
        service = _build("gmail", "v1", credentials=creds)
        result = build_profile_for_circle(
            args.person, [person_email], service,
            sample_size=args.sample_size,
            llm_backend=args.llm_backend,
            verbose=args.verbose,
            min_samples=MIN_SAMPLES_FOR_PERSON_PROFILE,
        )
        if "error" in result:
            print(f"[{args.person}] FAILED: {result['error']}")
            return 1
        person_dir = args.cache_dir / PERSON_CACHE_SUBDIR
        person_dir.mkdir(parents=True, exist_ok=True)
        path = person_dir / f"{args.person}.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[{args.person}] wrote {path}")
        print(f"[{args.person}] greeting: {result['profile']['typical_greeting']}")
        print(f"[{args.person}] signoff:  {result['profile']['typical_signoff']}")
        print(f"[{args.person}] register: {result['profile']['register']}")
        return 0

    buckets = bucket_people_by_circle(people_dir)
    print(f"Discovered circles: {sorted(buckets.keys())}")
    for c in sorted(buckets.keys()):
        print(f"  {c:25s} {len(buckets[c])} people")
    print()

    target_circles: list[str] = []
    if args.all:
        target_circles = sorted(buckets.keys())
    elif args.circle:
        if args.circle not in buckets:
            print(f"ERROR: circle {args.circle!r} not found. Available: "
                  f"{sorted(buckets.keys())}", file=sys.stderr)
            return 1
        target_circles = [args.circle]

    if args.dry_run:
        print("DRY RUN — would process:")
        for c in target_circles:
            print(f"  {c}: {len(buckets[c])} emails, sample up to {args.sample_size}")
        return 0

    from googleapiclient.discovery import build
    from agents.shared.google_oauth import get_credentials

    scopes = [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    creds = get_credentials(str(args.creds), str(args.token), scopes)
    service = build("gmail", "v1", credentials=creds)

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    for circle in target_circles:
        # Prefer circle-exclusive emails to avoid cross-circle voice bleed.
        # Fall back to all emails only if exclusive is too thin.
        exclusive = exclusive_emails_for_circle(buckets, circle)
        if len(exclusive) >= EXCLUSIVE_FALLBACK_THRESHOLD:
            emails_for_circle = exclusive
            source_label = f"exclusive ({len(exclusive)} of {len(buckets[circle])})"
        else:
            emails_for_circle = buckets[circle]
            source_label = f"all ({len(buckets[circle])}; only {len(exclusive)} exclusive < threshold)"
        print(f"[{circle}] sampling pool: {source_label}")
        result = build_profile_for_circle(
            circle, emails_for_circle, service,
            sample_size=args.sample_size,
            llm_backend=args.llm_backend,
            verbose=args.verbose,
        )
        if "error" in result:
            print(f"[{circle}] FAILED: {result['error']}")
            if args.verbose and "raw" in result:
                print(result["raw"][:1000])
            continue
        path = args.cache_dir / f"{circle}.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[{circle}] wrote {path}")
        print(f"[{circle}] greeting: {result['profile']['typical_greeting']}")
        print(f"[{circle}] signoff:  {result['profile']['typical_signoff']}")
        print(f"[{circle}] register: {result['profile']['register']}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
