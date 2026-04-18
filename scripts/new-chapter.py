#!/usr/bin/env python3
"""Scaffold a new `guide-v3/` chapter from the canonical template.

Usage:
    python scripts/new-chapter.py NN-slug --shape {agent|architecture|reference} [--title "Title"] [--difficulty {easy|moderate|hard|reference}]

Writes `guide-v3/NN-slug.md` with the H1, metadata line, TL;DR block,
and section headings for the chosen shape. Refuses to overwrite an
existing file.
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "guide-v3"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


AGENT_SKELETON = """\
# {title}

*Last updated: {today} · Reading time: ~20 min · Difficulty: {difficulty}*

**TL;DR**

- {{One-line capsule of what the agent does.}}
- {{The hardest technical thing about it.}}
- {{Optional: dependencies — residential proxy, TOTP, OAuth gating.}}
- {{Optional: time budget for first deploy.}}

## Meet the agent

{{One paragraph in the Busytown character's voice. This is the only
section in any agent chapter that speaks in character. Every other
section is back to the author's first-person.}}

## Why you'd want one — and why you might not

**You'd want {{the character}} if** {{the concrete problem shape it solves, with
specifics from your own life or fleet.}}

**You'd skip it if**:

- **{{Condition 1}}** — {{consequence}}.
- **{{Condition 2}}** — {{consequence}}.
- **{{Condition 3}}** — {{consequence}}.

## What makes this agent hard

{{3-5 paragraphs, each naming one specific technical challenge. Agent-specific
scar tissue, not generic LLM complaints.}}

## {{Body section — agent-specific}}

{{Examples from existing chapters: "The Google OAuth pattern", "The N-act
saga", "The routing boundary with {{other agent}}", "The mining pipeline".
Named incidents live here as their own subsection.}}

## Deployment walkthrough

The general seven-step arc from [Ch 08 — Your first agent](08-your-first-agent.md)
applies. Only the {{agent}}-specific deltas follow.

### Pre-step — {{prerequisite specific to this agent}}

{{e.g. "Residential proxy", "Google Cloud project setup", "Dropbox brain
must exist first".}}

### Steps 1-7 of the arc

- **Step 1 (Telegram bot)**: {{env var name, any fleet-default vs per-agent token notes}}.
- **Step 2 (bootstrap configs)**: {{which files, what to edit}}.
- **Step 3 (scripts)**: {{net-new authoring for this agent, or "all scripts already committed"}}.
- **Step 4 (manifest.json)**: {{any per-agent deltas}}.
- **Step 5 (host-cron registration)**: {{which CONTRACT_ENTRY lines, expected count}}.
- **Step 6 (deploy)**: {{any agent-specific flags}}.
- **Step 7 (install-host-cron.sh)**: {{which crons should land, expected count}}.

### Smoke test

{{Concrete one-liner commands the operator runs to verify after deploy.}}

## Pitfalls you'll hit

> 🧨 **Pitfall.** {{Short title.}} **Why:** {{one sentence — a past failure
> this codifies}}. **How to avoid:** {{one sentence — the remedy}}.

{{3-6 pitfalls. Each traces to a real incident from the commit log.}}

## See also

- [Ch 08 — Your first agent](08-your-first-agent.md) — the seven-step arc.
- [Ch {{N}} — {{Title}}]({{N}}-{{slug}}.md) — {{one-line pointer}}.
"""


ARCHITECTURE_SKELETON = """\
# {title}

*Last updated: {today} · Reading time: ~15 min · Difficulty: {difficulty}*

> **TL;DR.** {{Single paragraph summarising the concept, the shape of its
> implementation, and any cross-cutting idioms or layer counts the body
> will cover. Typically 3-5 sentences.}}

## Why {{concept}} matters

{{Opening thesis. Why does this concept exist? What failure mode does it
prevent? Grounded in a named incident when possible.}}

## {{Body section — concept-driven}}

{{Examples: "The two halves", "The six auth shapes", "The seven defense
layers", "The five components". The chapter's central structural claim
organises its sections.}}

### {{Sub-section per element}}

{{Each element gets its own subsection with consistent shape —
e.g. each auth shape has a "Used by" / "Token lifetime" / "Setup flow" /
"Canonical reference" block.}}

## Pitfalls

> 🧨 **Pitfall.** {{Title.}} **Why:** {{reason}}. **How to avoid:** {{remedy}}.

## See also

- [Ch {{N}} — {{Title}}]({{N}}-{{slug}}.md) — {{pointer}}.
"""


REFERENCE_SKELETON = """\
# {title}

*Last updated: {today} · Reading time: ~10 min · Difficulty: reference*

> **TL;DR.** {{One paragraph explaining what this chapter catalogs and
> how it's organised.}}

## Category 1 — {{name}}

{{Intro sentence.}}

| {{Header}} | {{Header}} | {{Header}} |
|------------|------------|------------|
| {{cell}}   | {{cell}}   | {{cell}}   |

### {{Optional sub-category}}

{{Group related entries under subcategories.}}

## See also

- [Ch {{N}} — {{Title}}]({{N}}-{{slug}}.md) — {{pointer}}.
"""


SKELETONS = {
    "agent": AGENT_SKELETON,
    "architecture": ARCHITECTURE_SKELETON,
    "reference": REFERENCE_SKELETON,
}

DEFAULT_DIFFICULTY = {
    "agent": "hard",
    "architecture": "moderate",
    "reference": "reference",
}


def slug_to_title(slug: str) -> str:
    """`22-new-chapter-name` → `New chapter name`. Caller can --title override."""
    # Strip leading NN-
    parts = slug.split("-", 1)
    if len(parts) == 2 and parts[0].isdigit():
        name = parts[1]
    else:
        name = slug
    words = name.replace("-", " ").split()
    if not words:
        return slug
    return " ".join([words[0].capitalize(), *words[1:]])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug", help="Chapter slug including number prefix, e.g. '22-new-chapter'")
    ap.add_argument("--shape", choices=list(SKELETONS), default="agent",
                    help="Chapter shape (default: agent)")
    ap.add_argument("--title", help="H1 title (default: derived from slug)")
    ap.add_argument("--difficulty", choices=["easy", "moderate", "hard", "reference"],
                    help="Difficulty rating (default: picked per shape)")
    args = ap.parse_args(argv)

    target = GUIDE / f"{args.slug}.md"
    if target.exists():
        print(f"refusing to overwrite: {target}", file=sys.stderr)
        return 1

    title = args.title or slug_to_title(args.slug)
    difficulty = args.difficulty or DEFAULT_DIFFICULTY[args.shape]
    today = date.today().isoformat()

    body = SKELETONS[args.shape].format(title=title, today=today, difficulty=difficulty)
    target.write_text(body, encoding="utf-8")

    print(f"created: {target.relative_to(REPO)}")
    print(f"  shape:      {args.shape}")
    print(f"  title:      {title}")
    print(f"  difficulty: {difficulty}")
    print(f"  date:       {today}")
    print()
    print("Next steps (per .claude/skills/guide-chapter/SKILL.md):")
    print("  1. Mine the history: python scripts/mine-history.py <subtree>")
    print("  2. Fill the skeleton, applying VOICE.md and WRITING_PRINCIPLES.md")
    print(f"  3. Audit: python scripts/audit-guide.py --chapter {args.slug} --severity P0,P1,P2")
    print("  4. Add to mkdocs.yml nav: + guide-v3/index.md TOC")
    print("  5. Build: python scripts/build-guide-site.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
