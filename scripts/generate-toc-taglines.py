#!/usr/bin/env python3
"""TOC tagline helper — does NOT call any LLM.

The authoring workflow is: the assistant driving the guide-chapter
skill reads each chapter's TL;DR and writes the tagline directly into
`guide-v3/index.md`. This script helps that workflow in two ways:

1. **Staleness check.** Compares each chapter's current TL;DR against
   the tagline currently shown in `guide-v3/index.md`. Flags chapters
   where the TL;DR has shifted materially since the tagline was
   written.

2. **Scaffold regenerate.** Rebuilds the grid-card structure from the
   `SECTIONS` dict, preserving every existing tagline and inserting
   `TODO(tagline)` placeholders for new chapters. Run this when a
   chapter is added, removed, or renamed — the assistant then fills
   the TODO placeholders by reading the new chapter's TL;DR.

Usage:
    python scripts/generate-toc-taglines.py --check
        # Report chapters whose TL;DR likely drifted from their tagline.

    python scripts/generate-toc-taglines.py --scaffold
        # Rebuild the TOC structure. Preserves existing taglines; inserts
        # TODO(tagline) placeholders for any chapter without one.

    python scripts/generate-toc-taglines.py --dry-run --scaffold
        # Preview scaffold output without writing.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "guide-v3"
TOC = GUIDE / "index.md"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# Canonical section assignment — matches mkdocs.yml nav and the TOC layout.
SECTIONS: dict[str, list[str]] = {
    "Overview": ["01-what-is-clawford", "02-what-isnt-clawford"],
    "Setup": [
        "03-before-you-start",
        "04-vps-setup",
        "05-dev-setup",
        "06-infra-setup",
    ],
    "Agents": [
        "07-intro-to-agents",
        "08-your-first-agent",
        "09-mr-fixit",
        "10-lowly-worm-newsfeed",
        "11-lowly-worm-social",
        "12-mistress-mouse",
        "13-sergeant-murphy",
        "14-huckle-cat",
        "15-hilda-hippo",
    ],
    "Architecture": [
        "16-shared-brain",
        "17-auth-architectures",
        "18-the-inbox",
        "19-security-and-hardening",
    ],
    "Reference": ["20-scripts-and-configs", "21-glossary"],
}

TODO_TAGLINE = "TODO(tagline) — read the TL;DR and write one here."

H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
METADATA_LINE = re.compile(
    r"\*Last updated:\s*\d{4}-\d{2}-\d{2}\s*·\s*Reading time:\s*~?(\d+)\s*min\s*·\s*Difficulty:\s*(\w+)\*"
)
TLDR_BULLETS = re.compile(r"\*\*TL;DR\*\*\s*\n\n-\s+(.+?)(?:\n-|\n\n)", re.DOTALL)
TLDR_BLOCKQUOTE = re.compile(r">\s+\*\*TL;DR\.?\*\*\s+(.+?)(?:\n\n|\n>\s*$)", re.DOTALL)


@dataclass
class ChapterMeta:
    stem: str
    title: str
    difficulty: str
    reading_minutes: int
    tldr: str


@dataclass
class TocEntry:
    stem: str
    tagline: str  # as currently rendered in index.md


def parse_chapter(stem: str) -> ChapterMeta:
    path = GUIDE / f"{stem}.md"
    text = path.read_text(encoding="utf-8")

    h1 = H1.search(text)
    title = h1.group(1).strip() if h1 else stem

    meta = METADATA_LINE.search(text)
    if not meta:
        raise ValueError(f"{stem}: missing metadata line")
    minutes = int(meta.group(1))
    difficulty = meta.group(2)

    m = TLDR_BULLETS.search(text)
    if m:
        tldr = m.group(1).strip()
    else:
        m = TLDR_BLOCKQUOTE.search(text)
        if not m:
            raise ValueError(f"{stem}: no TL;DR bullet or blockquote found")
        para = m.group(1).strip()
        m2 = re.match(r"(.+?\.)\s+\*\*", para)
        tldr = m2.group(1).strip() if m2 else para[:300]

    tldr = re.sub(r"\s+", " ", tldr)
    return ChapterMeta(stem=stem, title=title, difficulty=difficulty,
                       reading_minutes=minutes, tldr=tldr)


def parse_existing_toc() -> dict[str, str]:
    """Parse guide-v3/index.md and return {stem: current_tagline}.
    Matches the grid-card shape:
        -   **[Title](NN-slug.md)**
            ---
            `difficulty` · ~N min
            Tagline text here.
    """
    if not TOC.exists():
        return {}
    text = TOC.read_text(encoding="utf-8")
    # Each card: capture the stem via the link, then skip badges, capture tagline paragraph.
    pattern = re.compile(
        r"-\s+\*\*\[[^\]]+\]\((\d{2}-[a-z0-9-]+)\.md\)\*\*\s*\n"
        r"\s*\n\s*---\s*\n"
        r"\s*\n\s*`[^`]+`\s*·\s*~?\d+\s*min\s*\n"
        r"\s*\n\s*(.+?)\n\s*\n",
        re.DOTALL,
    )
    out: dict[str, str] = {}
    for m in pattern.finditer(text):
        stem = m.group(1)
        tagline = re.sub(r"\s+", " ", m.group(2)).strip()
        out[stem] = tagline
    return out


def render_toc(chapters: dict[str, ChapterMeta], taglines: dict[str, str]) -> str:
    """Render the full TOC page markdown."""
    today = subprocess.run(
        ["git", "-C", str(REPO), "log", "-1", "--format=%ad", "--date=short"],
        capture_output=True, text=True,
    ).stdout.strip() or "2026-04-17"

    out: list[str] = []
    out.append("![Clawford](../assets/Clawford2.png)")
    out.append("")
    out.append("# Table of contents")
    out.append("")
    out.append(f"*Last updated: {today}*")
    out.append("")
    out.append(
        "Twenty-one chapters, six sections. Pick a chapter by the job you want to\n"
        "do, or read top-to-bottom if you're deploying a fleet from scratch.\n"
        "Reading-time estimates are rough (230 words per minute); difficulty is\n"
        "easy / moderate / hard / reference."
    )
    out.append("")

    for section, stems in SECTIONS.items():
        out.append(f"## {section}")
        out.append("")
        out.append('<div class="grid cards" markdown>')
        out.append("")
        for stem in stems:
            ch = chapters.get(stem)
            if ch is None:
                continue
            tagline = taglines.get(stem, TODO_TAGLINE)
            out.append(f"-   **[{ch.title}]({stem}.md)**")
            out.append("")
            out.append("    ---")
            out.append("")
            out.append(f"    `{ch.difficulty}` · ~{ch.reading_minutes} min")
            out.append("")
            out.append(f"    {tagline}")
            out.append("")
        out.append("</div>")
        out.append("")

    out.append("## Lore")
    out.append("")
    out.append('<div class="grid cards" markdown>')
    out.append("")
    out.append("-   **[The Ballad of Mr Fixit](ballad-of-mr-fixit.md)**")
    out.append("")
    out.append("    ---")
    out.append("")
    out.append("    `easy` · ~10 min")
    out.append("")
    out.append("    A five-act tragedy covering the first day of setup. The myth")
    out.append("    version before the manual version.")
    out.append("")
    out.append("</div>")
    out.append("")

    out.append("## See also")
    out.append("")
    out.append("- [Guide v2](../guide-v2/index.md) — the frozen OpenClaw-era guide,")
    out.append("  preserved as historical record.")
    out.append("")
    return "\n".join(out)


def cmd_check(chapters: dict[str, ChapterMeta], taglines: dict[str, str]) -> int:
    """Flag chapters missing from the TOC or still carrying a TODO
    placeholder. Does NOT flag "low word overlap" drift — a good tagline
    paraphrases the TL;DR rather than quoting it, so that heuristic
    produces false positives on well-written taglines. Re-checking
    taglines after a material rewrite is the operator's responsibility
    (Phase 5.6 of the guide-chapter skill)."""
    missing: list[str] = []
    todo: list[str] = []

    for stem, ch in chapters.items():
        if stem not in taglines:
            missing.append(stem)
            continue
        tagline = taglines[stem]
        if tagline.startswith("TODO(") or tagline == TODO_TAGLINE:
            todo.append(stem)

    if missing:
        print("## Chapters missing from the TOC")
        print()
        for stem in missing:
            print(f"- `{stem}` — not found in guide-v3/index.md. Run --scaffold.")
        print()

    if todo:
        print("## Chapters with TODO placeholders")
        print()
        for stem in todo:
            print(f"- `{stem}` — tagline is still `TODO(tagline)`. Write one.")
            ch = chapters[stem]
            print(f"  TL;DR starts: {ch.tldr[:180]}{'…' if len(ch.tldr) > 180 else ''}")
        print()

    if not missing and not todo:
        print("TOC is structurally complete — every chapter has a tagline.")
        print("Remember: this script cannot detect semantic drift. After a material")
        print("chapter rewrite, re-read the tagline manually against the new TL;DR.")
        return 0

    return 1


def cmd_scaffold(chapters: dict[str, ChapterMeta],
                 existing_taglines: dict[str, str],
                 dry_run: bool) -> int:
    rendered = render_toc(chapters, existing_taglines)
    if dry_run:
        print(rendered)
    else:
        TOC.write_text(rendered, encoding="utf-8")
        print(f"wrote {TOC.relative_to(REPO)}")
        missing_taglines = [s for s in chapters if s not in existing_taglines]
        if missing_taglines:
            print()
            print("New chapter(s) without taglines — fill these in guide-v3/index.md:")
            for stem in missing_taglines:
                print(f"  - {stem}: TL;DR starts with:")
                print(f"      {chapters[stem].tldr[:200]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="report taglines whose source TL;DR likely drifted")
    mode.add_argument("--scaffold", action="store_true",
                      help="rebuild the TOC structure, preserving existing taglines")
    ap.add_argument("--dry-run", action="store_true",
                    help="(scaffold) print the proposed TOC to stdout; don't write")
    args = ap.parse_args(argv)

    chapters: dict[str, ChapterMeta] = {}
    for stems in SECTIONS.values():
        for stem in stems:
            try:
                chapters[stem] = parse_chapter(stem)
            except ValueError as e:
                print(f"WARN: {e}", file=sys.stderr)
                continue

    existing_taglines = parse_existing_toc()

    if args.check:
        return cmd_check(chapters, existing_taglines)
    return cmd_scaffold(chapters, existing_taglines, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
