#!/usr/bin/env python3
"""Editorial audit of guide-v3/*.md.

Runs the mechanical checks from the guide's editorial-review methodology.
Reports structured findings per chapter; does not modify files.

Exit codes:
  0 — no defects (or only P2/P3 style warnings)
  1 — P0 or P1 defects found
  2 — tool error
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Force UTF-8 stdout on Windows so emoji in guide text doesn't trip cp1252.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "guide-v3"

# ---------- data ----------

SEVERITY_ORDER = ["P0", "P1", "P2", "P3"]


@dataclass
class Finding:
    severity: str           # P0 / P1 / P2 / P3
    check: str              # short name
    line: int
    column: int
    message: str
    evidence: str = ""      # excerpt from line


@dataclass
class ChapterReport:
    path: Path
    findings: list[Finding] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.stem

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def by_severity(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {s: [] for s in SEVERITY_ORDER}
        for f in self.findings:
            out[f.severity].append(f)
        for s in out:
            out[s].sort(key=lambda f: (f.line, f.column))
        return out


# ---------- helpers ----------

CODE_FENCE = re.compile(r"^```")
INLINE_CODE = re.compile(r"`[^`\n]+`")
URL_RE = re.compile(r"https?://\S+")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def iter_lines(text: str) -> Iterable[tuple[int, str, bool]]:
    """Yield (1-based line number, line text, in_code_block)."""
    in_code = False
    for i, raw in enumerate(text.splitlines(), 1):
        if CODE_FENCE.match(raw):
            in_code = not in_code
            yield i, raw, True
            continue
        yield i, raw, in_code


def strip_inline(text: str) -> str:
    """Remove inline code spans and URLs from a single line for prose-only checks."""
    text = INLINE_CODE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    return text


def git_sha_exists(sha: str) -> bool:
    try:
        res = subprocess.run(
            ["git", "-C", str(REPO), "cat-file", "-e", sha],
            capture_output=True,
            timeout=5,
        )
        return res.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def known_chapters() -> set[str]:
    return {p.stem for p in GUIDE.glob("*.md")}


# ---------- checks ----------

# A git SHA is lowercase-hex, typically 7 or 40 chars. We only flag tokens
# that look like genuine git SHAs:
#   - inside backticks (code span)
#   - lowercase-hex only (case-sensitive match)
#   - length 7, 8, 10, 12, or 40 (common git abbreviations; skips 11/13/etc.
#     to avoid false positives on Azure error codes, hex strings, etc.)
SHA_LIKE = re.compile(r"`([0-9a-f]{7,40})`")
VALID_SHA_LENGTHS = {7, 8, 9, 10, 12, 40}


def check_commit_shas(report: ChapterReport, text: str) -> None:
    """Flag any commit SHA in prose. Policy: drop all SHAs fleet-wide."""
    for lineno, line, in_code in iter_lines(text):
        if in_code:
            continue
        for m in SHA_LIKE.finditer(line):
            sha = m.group(1)
            if len(sha) not in VALID_SHA_LENGTHS:
                continue
            if sha.isdigit():
                continue
            # Require proximity to a commit-y word to reduce false positives on
            # random lowercase-hex tokens.
            window = line[max(0, m.start() - 60): min(len(line), m.end() + 60)].lower()
            is_commit_context = any(
                word in window
                for word in ("commit", "sha", "hash", "revert", "landed in", "fix (", "revision")
            )
            exists = git_sha_exists(sha)
            if not is_commit_context and not exists:
                # Random lowercase-hex word — skip.
                continue
            if exists:
                report.add(Finding(
                    severity="P1",
                    check="commit-sha",
                    line=lineno,
                    column=m.start(),
                    message=f"commit SHA `{sha}` in prose (policy: drop all SHAs, cite dates instead)",
                    evidence=line.strip()[:120],
                ))
            else:
                report.add(Finding(
                    severity="P0",
                    check="broken-commit-sha",
                    line=lineno,
                    column=m.start(),
                    message=f"commit SHA `{sha}` not found in git log (broken reference)",
                    evidence=line.strip()[:120],
                ))


CHAPTER_PREFIX = re.compile(r"^(\d{2})-")


def check_markdown_links(report: ChapterReport, text: str) -> None:
    """Verify every relative .md link resolves to an existing file,
    and that [Ch N] display text matches the target chapter's number prefix."""
    chapters = known_chapters()
    for lineno, line, in_code in iter_lines(text):
        if in_code:
            continue
        for m in LINK_RE.finditer(line):
            display, target = m.group(1), m.group(2)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # Strip anchor
            file_part = target.split("#", 1)[0]
            if not file_part:
                continue
            # Only validate .md links inside guide-v3 or sibling dirs we know
            target_path = (report.path.parent / file_part).resolve()
            if target_path.suffix != ".md":
                continue
            if not target_path.exists():
                report.add(Finding(
                    severity="P0",
                    check="broken-link",
                    line=lineno,
                    column=m.start(),
                    message=f"broken link target `{target}` (display: {display!r})",
                    evidence=line.strip()[:120],
                ))
            # Ch 01 mis-link pattern: [Ch 01...](index.md) except in the TOC-link case
            if file_part == "index.md" and re.search(r"\bCh\s*0?1\b", display):
                report.add(Finding(
                    severity="P0",
                    check="ch01-points-to-index",
                    line=lineno,
                    column=m.start(),
                    message=f"link labelled as Ch 01 points to index.md (should be 01-what-is-clawford.md)",
                    evidence=line.strip()[:120],
                ))
            # Display-number-vs-target-number mismatch: "[Ch N ...](NN-slug.md)" where N != NN.
            # Catches stale chapter numbers in display text.
            display_ch_m = re.search(r"\bCh(?:apter)?\s*0?(\d{1,2})\b", display)
            target_prefix_m = CHAPTER_PREFIX.match(target_path.name)
            if display_ch_m and target_prefix_m:
                display_num = int(display_ch_m.group(1))
                target_num = int(target_prefix_m.group(1))
                if display_num != target_num:
                    report.add(Finding(
                        severity="P0",
                        check="chapter-number-mismatch",
                        line=lineno,
                        column=m.start(),
                        message=f"display says 'Ch {display_num}' but target is {target_path.name}",
                        evidence=line.strip()[:120],
                    ))


BARE_REFS = [
    (re.compile(r"(?<!\[)\bCh(?:apter)?\s+\d+\b"), "bare-chapter-ref"),
    (re.compile(r"(?<!\[)\bSafeguard\s+\d+\b"), "bare-safeguard-ref"),
    (re.compile(r"(?<!\[)\bPart\s+[IVX]+\b"), "bare-part-ref"),
    (re.compile(r"(?<!\[)\bAct\s+[IVX]+\b"), "bare-act-ref"),
    (re.compile(r"(?<!\[)\bTier\s+\d+\b"), "bare-tier-ref"),
]

# Chapters that own a concept are allowed to reference it without linking —
# the concept is grounded in-chapter, links to self are noise. Reference
# chapters (Ch 20 scripts-and-configs, Ch 21 glossary) also get exempted
# because they're terse lookups, not prose narrative.
_REFERENCE_CHAPTERS = {"20-scripts-and-configs", "21-glossary"}
BARE_REF_OWNER_EXEMPTIONS = {
    "bare-safeguard-ref": {"06-infra-setup", "19-security-and-hardening"} | _REFERENCE_CHAPTERS,
    "bare-tier-ref": {"06-infra-setup", "15-hilda-hippo", "17-auth-architectures"} | _REFERENCE_CHAPTERS,
    "bare-act-ref": {"15-hilda-hippo"} | _REFERENCE_CHAPTERS,
    "bare-part-ref": set(),
}


TABLE_ROW = re.compile(r"^\s*\|.*\|")
HEADING = re.compile(r"^\s*#{1,6}\s")


REF_NORMALIZE = re.compile(r"\s+")


def _normalize_ref(token: str) -> str:
    """Normalize 'Ch 6' / 'Chapter 06' / 'Ch  6' to 'Ch 6' for dedup."""
    t = REF_NORMALIZE.sub(" ", token.strip())
    t = t.replace("Chapter", "Ch").replace("chapter", "Ch")
    t = re.sub(r"Ch 0+", "Ch ", t)
    return t


def check_bare_references(report: ChapterReport, text: str) -> None:
    """Find cross-references in prose that aren't wrapped in markdown links.
    Looks for patterns like `Ch 6` or `Safeguard 10` that aren't inside [...] link text.

    Skips:
    - Chapters that own the concept (Ch 06 for Safeguards, Ch 15 for Costco Acts, etc.)
    - Table rows and headings — links in those contexts add visual noise
    - Subsequent bare mentions in a chapter where a linked reference to the
      same target already exists earlier in the same chapter (first-mention-only rule)
    """
    # First pass: collect every linked reference already in the chapter.
    # Format: normalized ref token -> earliest line it appears linked.
    linked_refs: dict[str, int] = {}
    for lineno, line, in_code in iter_lines(text):
        if in_code:
            continue
        for m in LINK_RE.finditer(line):
            display = m.group(1)
            # Look for "Ch N", "Safeguard N", "Act N", "Tier N", "Part N" in display
            for pattern, _ in BARE_REFS:
                for mm in pattern.finditer(display):
                    key = _normalize_ref(mm.group(0))
                    linked_refs.setdefault(key, lineno)

    for lineno, line, in_code in iter_lines(text):
        if in_code:
            continue
        if TABLE_ROW.match(line) or HEADING.match(line):
            continue
        prose = strip_inline(line)
        # Remove all link text and targets so we only see bare refs in prose
        prose_no_links = LINK_RE.sub(" ", prose)
        for pattern, name in BARE_REFS:
            owners = BARE_REF_OWNER_EXEMPTIONS.get(name, set())
            if report.name in owners:
                continue
            for m in pattern.finditer(prose_no_links):
                key = _normalize_ref(m.group(0))
                # Suppress if the same ref is already linked earlier in the chapter.
                first_linked = linked_refs.get(key)
                if first_linked is not None and first_linked <= lineno:
                    continue
                report.add(Finding(
                    severity="P2",
                    check=name,
                    line=lineno,
                    column=m.start(),
                    message=f"bare reference {m.group(0)!r} — consider wrapping in a markdown link",
                    evidence=line.strip()[:120],
                ))


RETIRED_SAFEGUARDS = {"8", "11"}


def check_retired_safeguards(report: ChapterReport, text: str) -> None:
    """Find Safeguard 8 / Safeguard 11 without 'retired' or 'tombstone' context
    in the surrounding paragraph (±3 lines on each side)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if CODE_FENCE.match(line):
            continue
        for m in re.finditer(r"Safeguard\s+(\d+)", line):
            if m.group(1) not in RETIRED_SAFEGUARDS:
                continue
            window = "\n".join(lines[max(0, i - 3): min(len(lines), i + 4)]).lower()
            if "retired" in window or "tombstone" in window:
                continue
            report.add(Finding(
                severity="P1",
                check="retired-safeguard-unlabelled",
                line=i + 1,
                column=m.start(),
                message=f"Safeguard {m.group(1)} mentioned without 'retired' / 'tombstone' context in surrounding paragraph",
                evidence=line.strip()[:120],
            ))


def check_fleet_size(report: ChapterReport, text: str) -> None:
    """Flag 'five agents' / 'five-agent' — fleet is six.
    Skips 'other five', 'remaining five', 'first five' (contextual references
    to five of six, not a fleet-size claim)."""
    for lineno, line, in_code in iter_lines(text):
        if in_code:
            continue
        for m in re.finditer(r"\bfive[- ]agents?\b", line, flags=re.IGNORECASE):
            preceding = line[max(0, m.start() - 25): m.start()].lower()
            if re.search(r"\b(other|remaining|first|next|last|all)\s*$", preceding):
                continue
            report.add(Finding(
                severity="P0",
                check="fleet-size-drift",
                line=lineno,
                column=m.start(),
                message=f"'{m.group(0)}' — fleet is six agents",
                evidence=line.strip()[:120],
            ))


def check_stale_pending(report: ChapterReport, text: str) -> None:
    for lineno, line, in_code in iter_lines(text):
        if in_code:
            continue
        for m in re.finditer(r"\*\(pending[^)]*\)\*", line, flags=re.IGNORECASE):
            report.add(Finding(
                severity="P1",
                check="stale-pending",
                line=lineno,
                column=m.start(),
                message=f"stale {m.group(0)!r} note — every referenced chapter now exists",
                evidence=line.strip()[:120],
            ))


def check_github_leaks(report: ChapterReport, text: str) -> None:
    """Flag github.com links that look like operator-repo references.
    Accepts templated placeholders (your-handle/your-repo), the bare
    github.com signup link, and external open-source project links
    (anything under a namespace not in OPERATOR_REPO_NAMESPACES).
    """
    operator_ns = re.compile(r"github\.com/(?:samsmith|openclaw-agents|clawford(?:-[a-z0-9-]+)?)\b", re.IGNORECASE)
    templated_ok = re.compile(r"your-handle|your-repo|<github>")
    for lineno, line, in_code in iter_lines(text):
        if in_code:
            continue
        for m in re.finditer(r"github\.com[/\w.-]*", line):
            target = m.group(0)
            ctx = line[max(0, m.start() - 30): m.end() + 30]
            if templated_ok.search(ctx):
                continue
            # Bare https://github.com account-signup link is OK
            if re.search(r"\[[^\]]+\]\(https://github\.com\)", line):
                continue
            # External open-source project link: not under operator namespaces.
            if not operator_ns.search(target):
                continue
            report.add(Finding(
                severity="P1",
                check="github-leak",
                line=lineno,
                column=m.start(),
                message=f"github.com reference to operator's repo (readers don't have repo access)",
                evidence=line.strip()[:120],
            ))


def check_dropbox_drift(report: ChapterReport, text: str) -> tuple[int, int]:
    """Count openclaw-backup vs clawford-backup mentions per chapter.
    Demoted to P3 because most dual-name mentions are intentional
    legacy-name caveats, not genuine drift."""
    oc = len(re.findall(r"openclaw-backup", text))
    cw = len(re.findall(r"clawford-backup", text))
    if oc and cw:
        report.add(Finding(
            severity="P3",
            check="dropbox-path-drift",
            line=0,
            column=0,
            message=f"chapter mentions both openclaw-backup ({oc}) and clawford-backup ({cw}) — verify intentional",
            evidence="",
        ))
    return oc, cw


WE_PRONOUN = re.compile(r"\b(we|our|us)\b", flags=re.IGNORECASE)
ITALIC_OR_QUOTED = re.compile(r"\*[^*\n]+\*|[\"“][^\"”\n]+[\"”]")


def check_we_pronoun(report: ChapterReport, text: str) -> None:
    """Flag 'we' / 'our' / 'us' in prose — guide voice is first-person singular.
    Skips pronouns inside italic spans (*…*) and quoted strings ("…") — those are
    verbatim content, not authorial voice."""
    for lineno, line, in_code in iter_lines(text):
        if in_code:
            continue
        prose = strip_inline(line)
        prose_no_links = LINK_RE.sub(" ", prose)
        # Replace italic / quoted content with spaces so we don't match inside quotes.
        prose_no_quotes = ITALIC_OR_QUOTED.sub(lambda m: " " * len(m.group(0)), prose_no_links)
        # Drop "US" (United States) and "USA"
        prose_clean = re.sub(r"\bUS(?:A|-\w+)?\b", " ", prose_no_quotes)
        for m in WE_PRONOUN.finditer(prose_clean):
            token = m.group(0)
            # Skip literal uppercase "US" (US Policy, US residential)
            if token == "US":
                continue
            report.add(Finding(
                severity="P2",
                check="forbidden-pronoun",
                line=lineno,
                column=m.start(),
                message=f"'{token}' pronoun — guide voice is first-person singular",
                evidence=line.strip()[:120],
            ))


ALL_CHECKS = [
    check_commit_shas,
    check_markdown_links,
    check_bare_references,
    check_retired_safeguards,
    check_fleet_size,
    check_stale_pending,
    check_github_leaks,
    check_we_pronoun,
]


# ---------- reporting ----------

def render_markdown(reports: list[ChapterReport], summary_only: bool = False) -> str:
    lines: list[str] = ["# Guide audit report", ""]
    total_counts = {s: 0 for s in SEVERITY_ORDER}
    for r in reports:
        for f in r.findings:
            total_counts[f.severity] += 1
    lines.append(f"**Totals**: P0={total_counts['P0']} P1={total_counts['P1']} "
                 f"P2={total_counts['P2']} P3={total_counts['P3']}")
    lines.append("")
    if summary_only:
        return "\n".join(lines)
    for r in reports:
        if not r.findings:
            continue
        lines.append(f"## {r.name}.md")
        lines.append("")
        buckets = r.by_severity()
        for sev in SEVERITY_ORDER:
            if not buckets[sev]:
                continue
            lines.append(f"### {sev}")
            lines.append("")
            lines.append("| Line | Check | Message | Evidence |")
            lines.append("|------|-------|---------|----------|")
            for f in buckets[sev]:
                loc = str(f.line) if f.line else "—"
                ev = f.evidence.replace("|", "\\|")[:80]
                msg = f.message.replace("|", "\\|")
                lines.append(f"| {loc} | {f.check} | {msg} | `{ev}` |")
            lines.append("")
    return "\n".join(lines)


def render_json(reports: list[ChapterReport]) -> str:
    payload = []
    for r in reports:
        payload.append({
            "chapter": r.name,
            "path": str(r.path.relative_to(REPO)),
            "findings": [
                {
                    "severity": f.severity,
                    "check": f.check,
                    "line": f.line,
                    "column": f.column,
                    "message": f.message,
                    "evidence": f.evidence,
                }
                for f in r.findings
            ],
        })
    return json.dumps(payload, indent=2)


# ---------- main ----------

def audit_chapter(path: Path) -> ChapterReport:
    report = ChapterReport(path=path)
    text = path.read_text(encoding="utf-8")
    for check in ALL_CHECKS:
        check(report, text)
    # Dropbox-drift check returns counts but we currently just emit the finding
    check_dropbox_drift(report, text)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument("--summary", action="store_true", help="emit severity totals only")
    ap.add_argument("--chapter", action="append", default=[],
                    help="restrict to chapter stem(s), e.g. 09-mr-fixit (can be repeated)")
    ap.add_argument("--severity", default="P0,P1,P2",
                    help="comma-separated severities to include (default P0,P1,P2)")
    args = ap.parse_args(argv)

    if not GUIDE.is_dir():
        print(f"guide directory not found: {GUIDE}", file=sys.stderr)
        return 2

    targets = sorted(GUIDE.glob("*.md"))
    if args.chapter:
        wanted = set(args.chapter)
        targets = [p for p in targets if p.stem in wanted]
        if not targets:
            print(f"no chapters matched: {args.chapter}", file=sys.stderr)
            return 2

    reports = [audit_chapter(p) for p in targets]

    wanted_sev = {s.strip() for s in args.severity.split(",")}
    for r in reports:
        r.findings = [f for f in r.findings if f.severity in wanted_sev]

    if args.json:
        print(render_json(reports))
    else:
        print(render_markdown(reports, summary_only=args.summary))

    has_p0_p1 = any(
        f.severity in ("P0", "P1")
        for r in reports
        for f in r.findings
    )
    return 1 if has_p0_p1 else 0


if __name__ == "__main__":
    sys.exit(main())
