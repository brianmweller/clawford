#!/usr/bin/env python3
"""OpenClaw Shared Brain — Health Check

Validates directory structure, seed files, Dropbox conflicts, and file sizes.
Usage: python scripts/validate.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_DIRS = [
    "people",
    "facts",
    "commitments",
    "tasks",
    "notes",
    "agents",
    "archive",
]

# Core files that must always exist (non-agent files)
REQUIRED_CORE_FILES = {
    "README.md": "# OpenClaw Shared Brain",
    "people/_template.md": "# {Full Name}",
    "commitments/active.md": "# Commitments \u2014 Active",
    "tasks/queue.md": "# Tasks \u2014 Queue",
    "notes/inbox.md": "# Notes \u2014 Inbox",
}

MAX_FILE_SIZE_KB = 500

# Top-level directories excluded from size warnings. deploy-backups/ and
# workspace-snapshots/ hold deploy.py tarballs and are large by design —
# the retention policy lives in agents/shared/deploy.py, not here.
SIZE_CHECK_EXCLUDE_DIRS = {"deploy-backups", "workspace-snapshots"}


def check_directories(root):
    """Check that all required directories exist."""
    results = []
    for d in REQUIRED_DIRS:
        path = root / d
        if path.is_dir():
            results.append(("PASS", "Directory exists: {}".format(d), ""))
        else:
            results.append(("FAIL", "Missing directory: {}".format(d), str(path)))
    return results


def check_files(root):
    """Check core files exist with correct headers."""
    results = []
    for rel_path, expected_header in REQUIRED_CORE_FILES.items():
        path = root / rel_path
        if not path.is_file():
            results.append(("FAIL", "Missing file: {}".format(rel_path), str(path)))
            continue
        try:
            first_line = path.read_text(encoding="utf-8").split("\n")[0].strip()
        except Exception as e:
            results.append(("FAIL", "Cannot read: {}".format(rel_path), str(e)))
            continue
        if first_line == expected_header:
            results.append(("PASS", "Valid header: {}".format(rel_path), ""))
        else:
            results.append((
                "FAIL",
                "Bad header: {}".format(rel_path),
                "expected: {!r}, got: {!r}".format(expected_header, first_line),
            ))
    # Check that at least one monthly facts file exists
    facts_files = list((root / "facts").glob("*.md"))
    if facts_files:
        results.append(("PASS", "Facts files found: {}".format(len(facts_files)), ""))
    else:
        results.append(("FAIL", "No facts files in facts/", ""))
    return results


def check_conflicts(root):
    """Check for Dropbox conflict files."""
    conflicts = list(root.rglob("*conflicted copy*"))
    if not conflicts:
        return [("PASS", "No Dropbox conflict files found", "")]
    results = []
    for p in conflicts:
        rel = p.relative_to(root)
        results.append(("WARN", "Dropbox conflict: {}".format(rel), str(p)))
    return results


def check_file_sizes(root):
    """Flag files over the size threshold for archival."""
    large = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in SIZE_CHECK_EXCLUDE_DIRS:
            continue
        size_kb = p.stat().st_size / 1024
        if size_kb > MAX_FILE_SIZE_KB:
            large.append((p, size_kb))
    if not large:
        return [("PASS", "No files over {}KB".format(MAX_FILE_SIZE_KB), "")]
    results = []
    for p, size_kb in large:
        rel = p.relative_to(root)
        results.append(("WARN", "Large file: {} ({:.0f}KB)".format(rel, size_kb), str(p)))
    return results


def print_report(all_results):
    """Print health report. Returns True if healthy (no failures)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("=" * 60)
    print("  OpenClaw Shared Brain \u2014 Health Report")
    print("  {}".format(now))
    print("  Root: {}".format(BRAIN_ROOT))
    print("=" * 60)

    counts = {"PASS": 0, "FAIL": 0, "WARN": 0}

    for section_name, results in all_results:
        print("\n## {}".format(section_name))
        for status, message, detail in results:
            counts[status] += 1
            icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]"}[status]
            print("  {} {}".format(icon, message))
            if detail:
                print("         {}".format(detail))

    print("\n" + "-" * 60)
    print("  PASS: {}  |  FAIL: {}  |  WARN: {}".format(
        counts["PASS"], counts["FAIL"], counts["WARN"]))

    if counts["FAIL"] > 0:
        print("  Status: UNHEALTHY")
        return False
    elif counts["WARN"] > 0:
        print("  Status: HEALTHY (with warnings)")
        return True
    else:
        print("  Status: HEALTHY")
        return True


def main():
    all_results = [
        ("Directories", check_directories(BRAIN_ROOT)),
        ("Core Files", check_files(BRAIN_ROOT)),
        ("Dropbox Conflicts", check_conflicts(BRAIN_ROOT)),
        ("File Sizes", check_file_sizes(BRAIN_ROOT)),
    ]
    healthy = print_report(all_results)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
