"""Manifest structural invariants — catch tracking gaps before they bite.

These tests walk every agents/*/manifest.json in the repo and assert:

  1. `load_manifest` succeeds.
  2. Every entry in `scripts[]` exists as a real file at the declared
     path under agents/<id>/.
  3. Every `.py` script in `scripts[]` ast.parses successfully.
  4. Every entry in `config_files[].src` exists as a real file under
     agents/<id>/.
  5. Every `python3 .../scripts/<name>` reference inside a cron
     message has `scripts/<name>` in that manifest's `scripts[]`. This
     catches the situation where heartbeat.py is called by a cron but
     deploy.py doesn't manage it.
  6. Every manifest's `telegram.bot_token_env` env var is declared in
     ops/docker-compose.yml `environment:` block. Catches R5-class
     silent failures where an agent's bot token never reaches the
     running container.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
COMPOSE_PATH = REPO_ROOT / "ops" / "docker-compose.yml"


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

    def test_every_config_file_has_source_or_template(self, manifest_path):
        """Every config_files entry must resolve to either the real file or
        an .example sibling. Real files are gitignored after the 2026-04-13
        PII sanitization — a fresh clone only has templates, which the
        operator turns into real files via --bootstrap-configs. A manifest
        that references neither is broken (Safeguard 10 exit 5)."""
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        broken = []
        for cf in mf.config_files:
            src = manifest_path.parent / cf.src
            template = manifest_path.parent / (cf.src + ".example")
            if not src.exists() and not template.exists():
                broken.append(cf.src)
        assert not broken, (
            f"{mf.agent_id}: manifest.config_files[] lists {len(broken)} files "
            f"with neither a real source nor an .example template: {broken}"
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

    def test_no_claude_cli_in_tracked_scripts(self, manifest_path):
        """No tracked Python script may subprocess-call `claude -p` for
        LLM inference. Per feedback_no_claude_cli_in_agents.md, scripts
        must use the openclaw subscription-backed provider (`openclaw
        infer model run`) instead.

        This is a grep-style lint — catches the specific anti-pattern
        `"claude", "-p"` in a subprocess argv list. Safe against merely
        mentioning "claude" in a comment.
        """
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        violators = []
        for script in mf.scripts:
            if not script.endswith(".py"):
                continue
            src = manifest_path.parent / script
            try:
                text = src.read_text(encoding="utf-8")
            except Exception:
                continue
            # Look for ["claude", "-p" or ["claude","-p" in an argv list
            for pattern in ('"claude", "-p"', '"claude","-p"', "'claude', '-p'", "'claude','-p'"):
                if pattern in text:
                    violators.append((script, pattern))
                    break
        assert not violators, (
            f"{mf.agent_id}: tracked scripts still subprocess-call `claude -p`: "
            f"{violators}. Replace with `openclaw infer model run --prompt ... --json`."
        )

    def test_no_narration_silence_clauses(self, manifest_path):
        """Cron messages must not tell the agent to "produce NO output".

        That phrasing invites the LLM to narrate "I produced no output"
        instead of literally returning an empty string. Burns ~25s/token
        budget per run silently. The replacement phrasing is "return
        EXACTLY the empty string (zero bytes, no narration)".

        This test is a lint, not a functional assertion — it prevents
        regression of the P5 audit fix.
        """
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        bad: list[tuple[str, str]] = []
        for cron in mf.crons:
            for phrase in ("produce NO output", "Produce NO output", "produce no output"):
                if phrase in cron.message:
                    bad.append((cron.name, phrase))
        assert not bad, (
            f"{mf.agent_id}: cron messages use narration-inducing phrase: {bad}. "
            f"Use 'return EXACTLY the empty string (zero bytes, no narration)' instead."
        )

    def test_bot_token_env_is_in_docker_compose(self, manifest_path):
        """Every agent's `telegram.bot_token_env` must appear as a key
        in ops/docker-compose.yml's `environment:` block. Catches
        R5-class silent failures: env var is in host `.env` but never
        gets passed through to the running container, so the agent's
        Telegram channel binding silently fails at delivery time."""
        deploy = _import_deploy()
        mf = deploy.load_manifest(manifest_path)
        token_env = mf.telegram_bot_token_env
        if not token_env:
            pytest.skip(f"{mf.agent_id} has no telegram.bot_token_env declared")

        assert COMPOSE_PATH.exists(), (
            f"ops/docker-compose.yml not found at {COMPOSE_PATH}"
        )
        compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

        # Look for a line like "      TOKEN_NAME: ${TOKEN_NAME..." inside
        # the environment block. Cheap regex rather than full YAML parse
        # to avoid a dependency.
        pattern = re.compile(
            rf"^\s{{6}}{re.escape(token_env)}\s*:\s*\$\{{",
            re.MULTILINE,
        )
        assert pattern.search(compose_text), (
            f"{mf.agent_id}: manifest declares telegram.bot_token_env="
            f"{token_env!r} but that key is not in ops/docker-compose.yml "
            f"environment: block. Either (a) add it to the compose file "
            f"environment: block alongside the others, or (b) update the "
            f"manifest to reference an env var that IS in the compose file."
        )
