#!/usr/bin/env python3
"""crlf-scan.py — detect (and optionally fix) CRLF line endings.

Reason to exist: SCPing files from Windows to Linux drags CRLF bytes
over the wire, producing spurious multi-thousand-line `git diff`s on
the Linux side even when the file content is identical. Run this
against a file or directory before/after an SCP deploy to catch the
pollution at its source.

Usage:
  python3 ops/scripts/crlf-scan.py <path>                 # report only
  python3 ops/scripts/crlf-scan.py <path> --fix           # normalize
  python3 ops/scripts/crlf-scan.py <dir>  --ext .py .md   # dir scan

Exit codes:
  0  clean (no CRLF found, or --fix ran successfully)
  1  CRLF detected and not fixed
  2  bad arguments / unreadable path
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}


def has_crlf(path: Path) -> bool:
    """Return True if the file contains any CRLF sequence.

    Reads the whole file as bytes — fine for source code; don't run
    against multi-GB binaries.
    """
    try:
        return b"\r\n" in path.read_bytes()
    except OSError:
        return False


def normalize(path: Path) -> int:
    """Replace CRLF with LF in place. Returns the number of CR bytes removed."""
    original = path.read_bytes()
    fixed = original.replace(b"\r\n", b"\n")
    saved = len(original) - len(fixed)
    if saved:
        path.write_bytes(fixed)
    return saved


def iter_files(root: Path, exts: list[str] | None) -> list[Path]:
    """Walk `root` (file or dir) and yield every candidate file.

    Skips hidden dirs (`.git`, `.venv`, etc.) and honors the optional
    extension filter. When `root` is a file, yields just that file
    regardless of extension — explicit user intent wins over filters.
    """
    if root.is_file():
        return [root]

    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        if exts and path.suffix not in exts:
            continue
        files.append(path)
    return files


def run(path: Path, fix: bool, exts: list[str] | None) -> int:
    """Core scanner entry point. Returns the exit code (0/1/2)."""
    if not path.exists():
        print(f"error: path does not exist: {path}", file=sys.stderr)
        return 2

    files = iter_files(path, exts)
    crlf_files = [p for p in files if has_crlf(p)]

    if not crlf_files:
        print(f"clean: no CRLF found ({len(files)} file(s) scanned)")
        return 0

    if fix:
        for p in crlf_files:
            saved = normalize(p)
            print(f"fixed: {p}  ({saved} CR byte(s) removed)")
        print(f"normalized {len(crlf_files)} file(s)")
        return 0

    for p in crlf_files:
        print(f"CRLF: {p}")
    print(
        f"found CRLF in {len(crlf_files)} file(s) — rerun with --fix",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Detect (and optionally fix) CRLF line endings.",
    )
    ap.add_argument("path", help="File or directory to scan")
    ap.add_argument(
        "--fix", action="store_true",
        help="Normalize CRLF → LF in place",
    )
    ap.add_argument(
        "--ext", nargs="+", default=None,
        help="When scanning a dir, only consider these extensions "
             "(e.g. --ext .py .md .sh)",
    )
    args = ap.parse_args()

    return run(Path(args.path), fix=args.fix, exts=args.ext)


if __name__ == "__main__":
    sys.exit(main())
