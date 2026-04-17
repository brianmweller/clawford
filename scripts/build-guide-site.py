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

# Links of the form `[label](../X/...)` where `X` is NOT one of these
# top-level dirs resolve outside the staged docs tree (into the
# repo's source code), and MkDocs can't find them. Rewrite those to
# absolute GitHub blob URLs so the rendered site gives readers a
# clickable pointer at the canonical source.
STAGED_TOP_LEVEL_DIRS = {"guide-v3", "guide-v2", "assets", "docs"}
GITHUB_BLOB_BASE = "https://github.com/samsmith/clawford/blob/master"

# guide-v2 is the frozen pre-liberation guide. Many of its chapters
# reference sibling v2 chapters that were never written before the
# freeze (07-3 through 11-glossary). Post-liberation those chapters
# all have equivalents in guide-v3; redirect dead v2-intra links
# there so the archival content stays navigable. 10-cli-reference
# has no v3 equivalent (the OpenClaw CLI was retired wholesale);
# point it at the glossary's retired-terms section as the closest
# stand-in.
GUIDE_V2_DEAD_LINK_REDIRECTS = {
    "07-3-mistress-mouse.md": "../guide-v3/12-mistress-mouse.md",
    "07-4-sergeant-murphy.md": "../guide-v3/13-sergeant-murphy.md",
    "07-5-huckle-cat.md": "../guide-v3/14-huckle-cat.md",
    "07-6-hilda-hippo.md": "../guide-v3/15-hilda-hippo.md",
    "07-7-auth-architectures.md": "../guide-v3/17-auth-architectures.md",
    "08-security-and-hardening.md": "../guide-v3/19-security-and-hardening.md",
    "09-scripts-and-configs.md": "../guide-v3/20-scripts-and-configs.md",
    "10-cli-reference.md": "../guide-v3/21-glossary.md#retired-terms",
    "11-glossary.md": "../guide-v3/21-glossary.md",
}


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

    _rewrite_out_of_tree_links()
    _rewrite_guide_v2_dead_links()

    # robots.txt — block all crawlers. Paired with the noindex meta
    # tag injected by overrides/main.html. MkDocs copies non-markdown
    # files from docs_dir straight to site_dir.
    (SRC / "robots.txt").write_text(
        "User-agent: *\nDisallow: /\n", encoding="utf-8",
    )


def _rewrite_guide_v2_dead_links():
    """Within _site_src/guide-v2/, rewrite sibling links to chapters
    that were never written (the frozen v2 guide) into links to their
    post-liberation guide-v3 equivalent."""
    pattern = re.compile(r"\[([^\]]+)\]\(([^)/]+\.md)(#[^)]*)?\)")

    def _fix(m: re.Match) -> str:
        label, target, fragment = m.group(1), m.group(2), m.group(3) or ""
        redirect = GUIDE_V2_DEAD_LINK_REDIRECTS.get(target)
        if redirect is None:
            return m.group(0)
        return f"[{label}]({redirect}{fragment})"

    for md_path in (SRC / "guide-v2").rglob("*.md"):
        text = md_path.read_text(encoding="utf-8")
        new_text = pattern.sub(_fix, text)
        if new_text != text:
            md_path.write_text(new_text, encoding="utf-8")


def _rewrite_out_of_tree_links():
    """Walk every staged .md file and rewrite `../<path>` links whose
    target lives outside the staged docs tree into absolute GitHub
    blob URLs. `../guide-v2/…`, `../assets/…`, `../docs/…` are all
    inside the staged tree and left alone."""
    pattern = re.compile(r"\[([^\]]+)\]\(\.\./([^)]+)\)")

    def _fix(m: re.Match) -> str:
        label, inner = m.group(1), m.group(2)
        first_segment = inner.split("/", 1)[0]
        if first_segment in STAGED_TOP_LEVEL_DIRS:
            return m.group(0)
        # External targets carry URL fragments (#anchor) cleanly — GitHub
        # blob URLs ignore unknown fragments, so passing them through
        # is a no-op at worst.
        return f"[{label}]({GITHUB_BLOB_BASE}/{inner})"

    for md_path in SRC.rglob("*.md"):
        text = md_path.read_text(encoding="utf-8")
        new_text = pattern.sub(_fix, text)
        if new_text != text:
            md_path.write_text(new_text, encoding="utf-8")


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
