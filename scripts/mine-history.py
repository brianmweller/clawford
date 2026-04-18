#!/usr/bin/env python3
"""Mine git log + plan files to surface narrative material for a guide chapter.

Usage:
    # Per-agent subtree:
    python scripts/mine-history.py agents/shopping

    # By commit-message keyword(s) — OR-joined via |:
    python scripts/mine-history.py --grep "P1|bubblewrap"

    # Restrict to a date range:
    python scripts/mine-history.py agents/shopping --since 2026-04-14 --until 2026-04-17

    # Machine-readable:
    python scripts/mine-history.py agents/shopping --json

Produces a markdown (default) or JSON report grouping commits by type,
flagging incident-shaped commits, and listing plan files whose mtime
falls in the window. Pipe the output into the draft as raw material.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLANS = Path.home() / ".claude" / "plans"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


TYPE_PREFIX = re.compile(r"^(feat|fix|docs|chore|refactor|test|perf|build|ci|style|revert)(\([^)]+\))?:\s*")

INCIDENT_HINTS = re.compile(
    r"\b(incident|broke|broken|bug|regression|silent\s+failure|revert|rollback|hotfix|emergency|outage|cascade)\b",
    re.IGNORECASE,
)

WIN_HINTS = re.compile(
    r"\b(\d+\s*(ms|s|seconds|sec|min|minutes|hours|hrs|day|days|x|\w+old|%)|faster|speedup|reduction|savings)\b",
    re.IGNORECASE,
)


@dataclass
class Commit:
    sha: str
    date: str   # YYYY-MM-DD
    subject: str
    type: str = "other"
    is_incident: bool = False
    is_win: bool = False


@dataclass
class Report:
    scope: str
    since: str | None
    until: str | None
    first_touch: str | None = None
    last_touch: str | None = None
    elapsed_days: int | None = None
    commits_by_type: dict[str, list[Commit]] = field(default_factory=lambda: defaultdict(list))
    incidents: list[Commit] = field(default_factory=list)
    wins: list[Commit] = field(default_factory=list)
    plan_files: list[dict] = field(default_factory=list)
    total_commits: int = 0


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print(f"git error: {' '.join(args)}\n{result.stderr}", file=sys.stderr)
        return ""
    return result.stdout


def collect_commits(
    path: str | None,
    grep: str | None,
    since: str | None,
    until: str | None,
) -> list[Commit]:
    args = ["log", "--date=short", "--pretty=format:%h\x1f%ad\x1f%s\x1e"]
    if since:
        args += [f"--since={since}"]
    if until:
        args += [f"--until={until}"]
    if grep:
        args += ["--grep", grep, "--extended-regexp"]
    if path:
        args += ["--", path]

    raw = run_git(*args)
    commits: list[Commit] = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) != 3:
            continue
        sha, date_, subject = parts
        m = TYPE_PREFIX.match(subject)
        ctype = m.group(1) if m else "other"
        commits.append(Commit(
            sha=sha,
            date=date_,
            subject=subject,
            type=ctype,
            is_incident=bool(INCIDENT_HINTS.search(subject)),
            is_win=bool(WIN_HINTS.search(subject)),
        ))
    return commits


def collect_plan_files(since: str | None, until: str | None) -> list[dict]:
    if not PLANS.is_dir():
        return []
    since_d = date.fromisoformat(since) if since else None
    until_d = date.fromisoformat(until) if until else None
    out: list[dict] = []
    for p in sorted(PLANS.glob("*.md")):
        mtime = datetime.fromtimestamp(p.stat().st_mtime).date()
        if since_d and mtime < since_d:
            continue
        if until_d and mtime > until_d:
            continue
        out.append({
            "name": p.name,
            "date": mtime.isoformat(),
            "size_bytes": p.stat().st_size,
        })
    return out


def build_report(
    scope: str,
    path: str | None,
    grep: str | None,
    since: str | None,
    until: str | None,
) -> Report:
    commits = collect_commits(path, grep, since, until)
    r = Report(scope=scope, since=since, until=until, total_commits=len(commits))

    if commits:
        dates = sorted(c.date for c in commits)
        r.first_touch = dates[0]
        r.last_touch = dates[-1]
        try:
            d0 = date.fromisoformat(dates[0])
            d1 = date.fromisoformat(dates[-1])
            r.elapsed_days = (d1 - d0).days
        except ValueError:
            pass

    for c in commits:
        r.commits_by_type[c.type].append(c)
        if c.is_incident:
            r.incidents.append(c)
        if c.is_win:
            r.wins.append(c)

    r.plan_files = collect_plan_files(since, until)
    return r


def render_markdown(r: Report) -> str:
    lines: list[str] = [f"# Chronology report: {r.scope}", ""]
    if r.since or r.until:
        lines.append(f"*Date filter: since={r.since or '—'}, until={r.until or '—'}*")
        lines.append("")
    lines.append(f"**{r.total_commits}** commits total.")
    if r.first_touch:
        lines.append(f"First touch: **{r.first_touch}**.")
    if r.last_touch:
        lines.append(f"Last touch: **{r.last_touch}**.")
    if r.elapsed_days is not None:
        lines.append(f"Elapsed calendar span: **{r.elapsed_days}** days.")
    lines.append("")

    if r.incidents:
        lines.append("## Incident-shaped commits")
        lines.append("")
        lines.append("Candidates for pitfall callouts. Each is a past failure worth narrating.")
        lines.append("")
        lines.append("| Date | Subject |")
        lines.append("|------|---------|")
        for c in r.incidents:
            subj = c.subject.replace("|", "\\|")
            lines.append(f"| {c.date} | {subj} |")
        lines.append("")

    if r.wins:
        lines.append("## Measurable wins")
        lines.append("")
        lines.append("Commits naming a quantified improvement. Pull these into the TL;DR or body.")
        lines.append("")
        lines.append("| Date | Subject |")
        lines.append("|------|---------|")
        for c in r.wins:
            subj = c.subject.replace("|", "\\|")
            lines.append(f"| {c.date} | {subj} |")
        lines.append("")

    lines.append("## Commits grouped by type")
    lines.append("")
    type_order = ["feat", "fix", "refactor", "docs", "chore", "perf", "test", "build", "ci", "style", "revert", "other"]
    for ctype in type_order:
        items = r.commits_by_type.get(ctype) or []
        if not items:
            continue
        lines.append(f"### `{ctype}` ({len(items)})")
        lines.append("")
        for c in items:
            subj = c.subject.replace(f"{ctype}: ", "", 1).replace(f"{ctype}(", f"`{ctype}(", 1)
            lines.append(f"- **{c.date}** — {subj}")
        lines.append("")

    if r.plan_files:
        lines.append("## Plan files in window")
        lines.append("")
        lines.append("Relevant plan files by mtime. Each carries granular chronology and effort estimates.")
        lines.append("")
        lines.append("| Date | Plan | Size |")
        lines.append("|------|------|------|")
        for p in r.plan_files:
            size_kb = p["size_bytes"] / 1024
            lines.append(f"| {p['date']} | `{p['name']}` | {size_kb:.1f} KB |")
        lines.append("")

    lines.append("## Next steps")
    lines.append("")
    lines.append("- Cross-reference every date in the draft against the commit dates above.")
    lines.append("- Draft each pitfall from the incident-shaped commits.")
    lines.append("- Pull the measurable wins into the TL;DR or the narrative fulcrum section.")
    lines.append("- Spot-check the most recent plan files for effort estimates and lessons learned.")
    lines.append("")
    return "\n".join(lines)


def render_json(r: Report) -> str:
    data = asdict(r)
    # defaultdict → dict, commits stay as dicts (already via asdict)
    data["commits_by_type"] = {k: v for k, v in r.commits_by_type.items()}
    return json.dumps(data, indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", help="Path / subtree to restrict the log to")
    ap.add_argument("--grep", help="git log --grep pattern (ERE)")
    ap.add_argument("--since", help="YYYY-MM-DD; filter to commits on/after this date")
    ap.add_argument("--until", help="YYYY-MM-DD; filter to commits on/before this date")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--json", action="store_true", help="shortcut for --format json")
    args = ap.parse_args(argv)

    if args.json:
        args.format = "json"

    if not args.path and not args.grep:
        ap.error("one of path / --grep is required")

    scope = args.path if args.path else f"grep={args.grep}"
    r = build_report(scope=scope, path=args.path, grep=args.grep,
                     since=args.since, until=args.until)

    if args.format == "json":
        print(render_json(r))
    else:
        print(render_markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
