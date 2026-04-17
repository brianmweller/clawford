#!/usr/bin/env python3
"""Stage guide-v3/, guide-v2/, docs/ballad + assets into _site_src/ and build.

Usage: python scripts/build-guide-site.py
Output: site/ (deployable to Cloudflare Pages, Netlify, etc.)
"""
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "_site_src"

DROP_FROM_V3 = {"99-unsorted-lessons.md"}


def _rmtree_retrying(path: Path, attempts: int = 6, delay: float = 0.5):
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def stage():
    if SRC.exists():
        _rmtree_retrying(SRC)
    SRC.mkdir()

    shutil.copytree(ROOT / "guide-v3", SRC / "guide-v3")
    for fname in DROP_FROM_V3:
        target = SRC / "guide-v3" / fname
        if target.exists():
            target.unlink()

    shutil.copytree(ROOT / "guide-v2", SRC / "guide-v2")
    shutil.copytree(ROOT / "assets", SRC / "assets")

    docs_out = SRC / "docs"
    docs_out.mkdir()
    shutil.copy2(ROOT / "docs" / "ballad-of-mr-fixit.md", docs_out / "ballad-of-mr-fixit.md")

    home = (ROOT / "guide-v3" / "index.md").read_text(encoding="utf-8")
    home = home.replace("../assets/", "assets/")

    def fix_link(m):
        label, target = m.group(1), m.group(2)
        if target.startswith(("http", "guide-v3/", "assets/", "#", "mailto:")):
            return m.group(0)
        if target.startswith("../"):
            inner = target[3:]
            if inner.startswith(("guide-v2/", "docs/")) and inner.endswith(".md"):
                return f"[{label}]({inner[:-3]}/)"
            if inner.startswith(("guide-v2/", "docs/")):
                return f"[{label}]({inner})"
            return m.group(0)
        if target.endswith(".md"):
            return f"[{label}](guide-v3/{target[:-3]}/)"
        return m.group(0)

    home = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", fix_link, home)
    (SRC / "index.md").write_text(home, encoding="utf-8")


def build():
    subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--clean"],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    stage()
    build()
    print(f"Built site/ from {SRC}")
