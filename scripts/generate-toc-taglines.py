#!/usr/bin/env python3
"""Regenerate the TOC taglines in `guide-v3/index.md` from each chapter's
current TL;DR.

Called after material chapter rewrites to keep the TOC taglines in sync
with the canonical summaries inside each chapter. The script:

  1. Reads every `guide-v3/NN-*.md`.
  2. Extracts the chapter's first TL;DR bullet (or the TL;DR blockquote
     for architecture-shape chapters).
  3. Calls `codex infer` (the project's LLM broker) to distill the TL;DR
     into a ~100-character tagline that lands the chapter's thesis.
  4. Writes the updated `guide-v3/index.md` with fresh taglines, grouped
     by the canonical section headings (Overview / Setup / Agents /
     Architecture / Reference / Lore).

Usage:
    python scripts/generate-toc-taglines.py              # regenerate all
    python scripts/generate-toc-taglines.py --dry-run    # preview only
    python scripts/generate-toc-taglines.py --chapter 15-hilda-hippo  # one
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
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

TAGLINE_PROMPT = """\
You are writing a one-sentence tagline for a technical-guide chapter,
to appear on the guide's table-of-contents page underneath the chapter
title.

Constraints:
- 80-130 characters. Hard cap 130.
- Lead with a concrete noun phrase, not a verb. Avoid "This chapter…",
  "A guide to…", "Covers…".
- No first-person pronouns. No second-person pronouns. No "we/our/us".
- If the chapter is an agent chapter, name the agent's actual job, not
  the abstract concept.
- If the chapter has a named incident or scar story, name it briefly.
- Match the voice of the source TL;DR — dry, technical, scar-tissue.

Input: the chapter's first TL;DR bullet.

Output: the tagline, and ONLY the tagline. No quotes, no markdown, no
preamble like "Here's the tagline:".

TL;DR source:
{tldr}
"""


LAST_UPDATED = re.compile(r"^\*Last updated:[^\n]+\*", re.MULTILINE)
H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
METADATA_LINE = re.compile(
    r"\*Last updated:\s*\d{4}-\d{2}-\d{2}\s*·\s*Reading time:\s*~?(\d+)\s*min\s*·\s*Difficulty:\s*(\w+)\*"
)
# First TL;DR bullet: find `**TL;DR**` (or `> **TL;DR.** ...`) then grab
# the first bullet / sentence after.
TLDR_BULLETS = re.compile(r"\*\*TL;DR\*\*\s*\n\n-\s+(.+?)(?:\n-|\n\n)", re.DOTALL)
TLDR_BLOCKQUOTE = re.compile(r">\s+\*\*TL;DR\.?\*\*\s+(.+?)(?:\n\n|\n>\s*$)", re.DOTALL)


@dataclass
class ChapterMeta:
    stem: str
    title: str
    difficulty: str
    reading_minutes: int
    tldr: str  # first TL;DR bullet or first sentence of TL;DR blockquote
    tagline: str = ""


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

    # Try the bullet-list shape first; fall back to the blockquote shape.
    m = TLDR_BULLETS.search(text)
    if m:
        tldr = m.group(1).strip()
    else:
        m = TLDR_BLOCKQUOTE.search(text)
        if not m:
            raise ValueError(f"{stem}: no TL;DR bullet or blockquote found")
        # Grab the first sentence or ~200 chars
        para = m.group(1).strip()
        # Split on first period followed by whitespace
        m2 = re.match(r"(.+?\.)\s+\*\*", para)
        if m2:
            tldr = m2.group(1).strip()
        else:
            tldr = para[:300]

    # Strip trailing bold continuation / excess markdown
    tldr = re.sub(r"\s+", " ", tldr)

    return ChapterMeta(
        stem=stem,
        title=title,
        difficulty=difficulty,
        reading_minutes=minutes,
        tldr=tldr,
    )


def call_codex_infer(prompt: str, timeout: int = 60) -> str | None:
    """Invoke `codex infer` to get a single-line tagline.
    Returns None if codex isn't available on the path (operator should
    run this on a box that has it — typically the VPS or a laptop with
    ChatGPT Plus via Codex OAuth)."""
    if not shutil.which("codex"):
        return None
    try:
        result = subprocess.run(
            ["codex", "infer", "--timeout", str(timeout)],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout + 10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    # Codex sometimes wraps output; strip quotes/whitespace.
    line = result.stdout.strip().strip('"').strip("'").strip()
    return line or None


def render_toc(chapters: dict[str, ChapterMeta]) -> str:
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
            ch = chapters[stem]
            # Strip any leading "NN — " from the title if it snuck in
            display = ch.title
            out.append(f"-   **[{display}]({stem}.md)**")
            out.append("")
            out.append("    ---")
            out.append("")
            out.append(f"    `{ch.difficulty}` · ~{ch.reading_minutes} min")
            out.append("")
            out.append(f"    {ch.tagline}")
            out.append("")
        out.append("</div>")
        out.append("")

    out.append("## Lore")
    out.append("")
    out.append('<div class="grid cards" markdown>')
    out.append("")
    out.append("-   **[The Ballad of Mr Fixit](../docs/ballad-of-mr-fixit.md)**")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the proposed TOC to stdout; don't write")
    ap.add_argument("--chapter", action="append", default=[],
                    help="only regenerate tagline for these chapter stems (repeatable)")
    ap.add_argument("--taglines-json", type=Path,
                    help="JSON file to source taglines from (skip LLM entirely)")
    args = ap.parse_args(argv)

    # Gather chapter metadata
    chapters: dict[str, ChapterMeta] = {}
    for stems in SECTIONS.values():
        for stem in stems:
            try:
                chapters[stem] = parse_chapter(stem)
            except ValueError as e:
                print(f"WARN: {e}", file=sys.stderr)
                continue

    # Generate or load taglines
    tagline_cache: dict[str, str] = {}
    if args.taglines_json and args.taglines_json.exists():
        tagline_cache = json.loads(args.taglines_json.read_text(encoding="utf-8"))

    target_stems = set(args.chapter) if args.chapter else set(chapters.keys())
    codex_available = shutil.which("codex") is not None
    if not codex_available and not args.taglines_json:
        print("WARN: `codex` not found on PATH — falling back to first TL;DR bullet verbatim.", file=sys.stderr)

    for stem, ch in chapters.items():
        if stem not in target_stems and stem in tagline_cache:
            ch.tagline = tagline_cache[stem]
            continue
        if stem in tagline_cache and stem not in args.chapter:
            ch.tagline = tagline_cache[stem]
            continue
        if codex_available:
            prompt = TAGLINE_PROMPT.format(tldr=ch.tldr)
            tagline = call_codex_infer(prompt)
            if tagline:
                ch.tagline = tagline
                print(f"  {stem}: {tagline}")
                continue
        # Fallback: trim the TL;DR to ~120 chars
        ch.tagline = (ch.tldr[:117] + "…") if len(ch.tldr) > 120 else ch.tldr
        print(f"  {stem}: [fallback] {ch.tagline}")

    rendered = render_toc(chapters)
    if args.dry_run:
        print(rendered)
    else:
        TOC.write_text(rendered, encoding="utf-8")
        print(f"wrote {TOC.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
