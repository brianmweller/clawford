#!/usr/bin/env python3
"""draft-compose.py — assemble a draft email reply for a known recipient.

Dry-run CLI for exercising the compose pipeline end-to-end against real
brain data + a real LLM. Production version (invoked by Gmail push webhook)
will swap the LLM backend to agents/shared/llm.py (codex). For now this
shells out to `claude -p` to keep the first run hands-on.

Inputs:
  --person-slug       slug of person in ~/Dropbox/openclaw-backup/people/
  --inbound           path to JSON file: {from_name, from_email, subject,
                      body, received_at}
  --history           optional path to JSON list of prior thread messages
                      [{from, body, date}, ...]
  --print-prompt-only don't call the LLM; just print the built prompt
  --llm-backend       "claude-cli" (default) or "stdout" (skip LLM, echo prompt)

Output:
  Prints voice calibration, the full LLM prompt, the LLM response, and the
  parsed draft/reasoning/citations. No writes.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


# Make agents.* imports work
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))
# Sibling compose_lib.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.shared.brain import dropbox_brain_root                  # noqa: E402
from agents.shared.context_builder import build_recipient_context  # noqa: E402
from agents.shared.facts import load_facts_for_subject             # noqa: E402
from agents.shared.voice import compose_voice_guidance             # noqa: E402
from compose_lib import build_compose_prompt, parse_compose_result  # noqa: E402


BRAIN_ROOT = dropbox_brain_root()

_FIELD_RE = re.compile(r"^\s*-\s*\*\*([\w_]+)(?::\*\*|\*\*:)\s*(.*)$")


def load_person(slug: str) -> dict:
    path = BRAIN_ROOT / "people" / f"{slug}.md"
    if not path.exists():
        raise SystemExit(f"Person file not found: {path}")
    text = path.read_text(encoding="utf-8")
    person = {"slug": slug}
    if text.startswith("# "):
        person["full_name"] = text.split("\n", 1)[0][2:].strip()
    for line in text.splitlines():
        m = _FIELD_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in {"—", "-", ""}:
            val = None
        person[key] = val
    for num in ("social_distance", "power_differential"):
        v = person.get(num)
        if isinstance(v, str):
            try:
                person[num] = float(v)
            except ValueError:
                pass
    if "circles" in person and isinstance(person["circles"], str):
        person["circles"] = [c.strip() for c in person["circles"].split(",")]
    return person


def call_claude_cli(prompt: str, timeout: int = 180) -> str:
    result = subprocess.run(
        ["claude", "-p"],
        input=prompt,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8",
    )
    if result.returncode != 0:
        return json.dumps({"error": f"claude cli exited {result.returncode}: {result.stderr.strip()[:500]}"})
    return result.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--person-slug", required=True)
    ap.add_argument("--inbound", required=True)
    ap.add_argument("--history")
    ap.add_argument("--facts-dir", help="Override brain/facts path (for dry-run testing)")
    ap.add_argument("--print-prompt-only", action="store_true")
    ap.add_argument("--llm-backend", default="claude-cli", choices=["claude-cli", "stdout"])
    args = ap.parse_args()

    person = load_person(args.person_slug)
    facts_dir = Path(args.facts_dir) if args.facts_dir else BRAIN_ROOT / "facts"
    facts = load_facts_for_subject(args.person_slug, facts_dir)
    inbound = json.loads(Path(args.inbound).read_text(encoding="utf-8"))
    history = json.loads(Path(args.history).read_text(encoding="utf-8")) if args.history else []

    ctx = build_recipient_context(
        recipient_person=person,
        all_facts=facts,
        email_history=history,
    )

    # Hardcoded dimensions for dry-run; production will come from an LLM
    # pre-pass classifier (agents/shared/inbound_act.py — not yet built).
    inbound_act = {
        "intent": "thank",
        "imposition": 0.1,
        "audience_shape": "one_to_one",
        "thread_position": "replying",
        "expected_response": "fyi_only",
        "sensitivity": "personal",
        "emotional_valence": "sensitive",
        "time_pressure": "late_reply",
    }

    voice = compose_voice_guidance(
        recipient=person,
        inbound_act=inbound_act,
        platform="gmail",
    )
    prompt = build_compose_prompt(ctx, voice, inbound)

    print("=" * 72)
    print("RECIPIENT")
    print("=" * 72)
    for k in ("full_name", "slug", "relationship_type", "circles", "tone",
              "communication_direction", "social_distance", "power_differential"):
        print(f"  {k}: {person.get(k)}")
    print()
    print("=" * 72)
    print("AUDIENCE + FACT FILTERING")
    print("=" * 72)
    print(f"  target_audiences:  {ctx.target_audiences}")
    print(f"  facts_loaded:      {len(facts)}")
    print(f"  facts_shareable:   {len(ctx.facts_shareable)}")
    print(f"  facts_blocked:     {len(ctx.facts_blocked)}")
    print()
    print("=" * 72)
    print("VOICE CALIBRATION")
    print("=" * 72)
    for k in ("register", "politeness_strategy", "politeness_weight",
              "direction", "power_description", "distance_description"):
        print(f"  {k}: {voice[k]}")

    if args.print_prompt_only:
        print()
        print("=" * 72)
        print("PROMPT")
        print("=" * 72)
        print(prompt)
        return 0

    if args.llm_backend == "stdout":
        print()
        print("=" * 72)
        print("PROMPT (stdout backend — no LLM call)")
        print("=" * 72)
        print(prompt)
        return 0

    print()
    print("=" * 72)
    print("CALLING LLM (claude -p)")
    print("=" * 72)
    llm_text = call_claude_cli(prompt)
    print(llm_text)

    shareable_ids = {f["id"] for f in ctx.facts_shareable}
    parsed = parse_compose_result(llm_text, shareable_ids=shareable_ids)

    print()
    print("=" * 72)
    if "error" in parsed:
        print(f"PARSE ERROR: {parsed['error']}")
        print("-" * 72)
        print(parsed.get("raw", "")[:4000])
        return 1

    if parsed["reply_needed"]:
        print("VERDICT: reply_needed=TRUE — would create Gmail draft")
        print("=" * 72)
        print(f"Objective:        {parsed['objective']}")
        print(f"Leverage:         {parsed['leverage']}")
        print(f"Strategy:         {parsed['strategy']}")
        print()
        print("DRAFT:")
        print("-" * 72)
        print(parsed["draft_text"])
        print("-" * 72)
        print(f"Telegram: {parsed['reasoning_summary']}")
    else:
        print("VERDICT: reply_needed=FALSE — no Gmail draft, Telegram FYI only")
        print("=" * 72)
        print(f"Objective (of silence): {parsed['objective']}")
        print(f"Leverage:               {parsed['leverage']}")
        print(f"Strategy:               {parsed['strategy']}")
        print(f"Recipient model:        {parsed['recipient_model']}")
        print()
        print("TELEGRAM FYI:")
        print("-" * 72)
        print(parsed["no_reply_fyi"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
