"""Manifest structural invariants — catch tracking gaps before they bite.

These tests walk every agents/*/manifest.json in the repo and assert:

  1. `load_manifest` succeeds.
  2. Every entry in `scripts[]` exists as a real file at the declared
     path under agents/<id>/.
  3. Every entry in `config_files[].src` exists as a real file under
     agents/<id>/.
  4. Every `python3 .../scripts/<name>` reference inside a cron
     message has `scripts/<name>` in that manifest's `scripts[]`. This
     catches the situation where heartbeat.py is called by a cron but
     deploy.py doesn't manage it.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
AGENTS_DIR = REPO_ROOT / "agents"


def _import_deploy():
    for mod in list(sys.modules):
        if mod == "deploy" or mod.startswith("deploy."):
            del sys.modules[mod]
    shared_dir = Path(__file__).resolve().parent.parent
    if str(shared_dir) not in sys.path:
        sys.path.insert(0, str(shared_dir))
    import deploy  # type: ignore
    return deploy


def _manifest_paths() -> list[Path]:
    """Return every agents/*/manifest.json in the repo, excluding shared/."""
    out = []
    for p in sorted(AGENTS_DIR.iterdir()):
        if not p.is_dir() or p.name in ("shared", "_templates") or p.name.startswith("."):
            continue
        mf = p / "manifest.json"
        if mf.exists():
            out.append(mf)
    return out


# Parameterize each manifest so a failure points at the offending agent.
@pytest.mark.parametrize(
    "manifest_path", _manifest_paths(), ids=lambda p: p.parent.name
)
class TestManifestInvariants:

    def test_manifest_loads(self, manifest_path):
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        assert mf.agent_id == manifest_path.parent.name

    def test_every_script_exists(self, manifest_path):
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        missing = []
        for script in mf.scripts:
            src = manifest_path.parent / script
            if not src.exists():
                missing.append(script)
        assert not missing, (
            f"{mf.agent_id}: manifest.scripts[] lists {len(missing)} files "
            f"that don't exist under agents/{mf.agent_id}/: {missing}"
        )

    def test_every_python_script_parses(self, manifest_path):
        """Every .py script in manifest.scripts[] must be a valid
        Python source file. Catches syntax errors in rescued/imported
        scripts before they reach production."""
        import ast
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        broken: list[tuple[str, str]] = []
        for script in mf.scripts:
            if not script.endswith(".py"):
                continue
            src = manifest_path.parent / script
            try:
                ast.parse(src.read_text(encoding="utf-8"))
            except SyntaxError as e:
                broken.append((script, f"{e.__class__.__name__}: {e}"))
        assert not broken, (
            f"{mf.agent_id}: {len(broken)} tracked Python scripts failed "
            f"to parse: {broken}"
        )

    def test_every_config_file_exists(self, manifest_path):
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        missing = []
        for cf in mf.config_files:
            src = manifest_path.parent / cf.src
            if not src.exists():
                missing.append(cf.src)
        assert not missing, (
            f"{mf.agent_id}: manifest.config_files[] lists {len(missing)} files "
            f"that don't exist: {missing}"
        )

    def test_scripts_referenced_by_crons_are_tracked(self, manifest_path):
        """Catches heartbeat.py-style gaps: cron message calls a script
        by path but the manifest's scripts[] list doesn't include it,
        so deploy.py doesn't manage the file."""
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        tracked_script_names = {
            Path(s).name for s in mf.scripts
        }

        # Pattern: python3 (optional quotes) /.../scripts/<name>.py
        pattern = re.compile(r"scripts/([A-Za-z0-9_\-]+\.(?:py|sh))")
        referenced: set[str] = set()
        for cron in mf.crons:
            for match in pattern.finditer(cron.message):
                referenced.add(match.group(1))

        missing = sorted(
            name for name in referenced
            if name not in tracked_script_names
            and (manifest_path.parent / "scripts" / name).exists()
        )
        assert not missing, (
            f"{mf.agent_id}: cron messages reference scripts not in "
            f"manifest.scripts[]: {missing}. Either add them to scripts[] "
            f"or stop calling them from cron messages."
        )
