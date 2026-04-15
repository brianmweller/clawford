"""Behavioral tests for install-host-cron.sh's disabled-agents mechanism.

Motivation: retire.sh used to do crontab filtering by hand, but any
re-run of install-host-cron.sh would restore the retired agent's crons
because the installer had no concept of "this agent is retired, skip
it". The mechanism tested here closes that gap.

Contract:

  - The installer reads `${DISABLED_AGENTS_FILE}` (default
    `$HOME/.openclaw/disabled-agents.txt`). One agent id per line.
    Lines starting with `#` and blank lines are ignored. Leading /
    trailing whitespace is stripped.
  - If the file is missing, the installer behaves exactly as before.
  - If an agent id X appears in the file, the installer:
      1. Skips installing any DIRECT_ENTRIES whose marker equals X or
         starts with "X-".
      2. Skips installing any CONTRACT_ENTRIES whose logname equals X
         or starts with "X-".
      3. Evicts any existing matching crontab lines in the same sweep
         that handles STALE_MARKERS / DRIFT_MARKERS.
  - Matching is prefix + hyphen-boundary — "fix-it" matches both
    "fix-it" itself and anything starting with "fix-it-". It does NOT
    match "fix-itchy" (no hyphen boundary after "fix-it"). Operators
    must spell out full agent ids; a partial id like "fix" would
    over-match onto "fix-it-*" entries because the installer has no
    canonical list of agent owners to disambiguate.

The tests run install-host-cron.sh in a subprocess with a stubbed
`crontab` shim on PATH (reused from test_install_host_cron.py's
harness) and a stubbed HOME so the installer reads a temp
disabled-agents.txt instead of the operator's real file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = REPO_ROOT / "ops" / "scripts" / "install-host-cron.sh"
CONTRACT_WRAPPER = REPO_ROOT / "ops" / "scripts" / "script-contract-host.sh"




pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="install-host-cron.sh requires bash on a POSIX-like environment",
)


def _write_fake_crontab(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake `crontab` shim that reads/writes a temp state file
    instead of the real crontab. Returns (bin_dir, state_file).

    The write path uses a temp file + atomic rename instead of
    `cat > "$STATE"` to avoid a TOCTOU race in pipelines like
    `{ crontab -l; ...; } | crontab -`. With a bare `>` redirect,
    the write side truncates STATE at pipeline setup time, causing
    the read side (`crontab -l`) to see an empty file. The temp-file
    variant defers the rename until after stdin has been drained,
    so the reader sees the unmodified content.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_file = tmp_path / "crontab-state.txt"
    state_file.touch()

    shim = bin_dir / "crontab"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'STATE="{state_file}"\n'
        'if [[ "${1:-}" == "-l" ]]; then\n'
        '  cat "$STATE" 2>/dev/null || true\n'
        '  exit 0\n'
        'fi\n'
        'if [[ "${1:-}" == "-" ]]; then\n'
        '  tmp=$(mktemp)\n'
        '  cat > "$tmp"\n'
        '  mv "$tmp" "$STATE"\n'
        '  exit 0\n'
        'fi\n'
        'echo "fake crontab: unsupported args: $*" >&2\n'
        'exit 1\n'
    )
    shim.chmod(0o755)
    return bin_dir, state_file


def _run_installer(
    bin_dir: Path,
    fake_home: Path,
    disabled_file: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run install-host-cron.sh with a stubbed crontab + HOME env.

    If `disabled_file` is given, it is passed to the installer via the
    DISABLED_AGENTS_FILE env var so the test doesn't have to care where
    the installer's default path lives.
    """
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(fake_home)
    if disabled_file is not None:
        env["DISABLED_AGENTS_FILE"] = str(disabled_file)
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    return home


# ─── Baseline: no file = install everything ─────────────────────────


def test_no_disabled_file_installs_everything(tmp_path):
    """Absent disabled-agents.txt, the installer behaves exactly as it
    always did — every DIRECT and CONTRACT entry lands in the crontab.
    """
    bin_dir, state_file = _write_fake_crontab(tmp_path)
    home = _fake_home(tmp_path)
    missing = home / ".openclaw" / "disabled-agents.txt"
    assert not missing.exists()

    result = _run_installer(bin_dir, home, missing)
    assert result.returncode == 0, (
        f"installer exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    )

    post = state_file.read_text()
    # Representative entries from each family should all be present.
    assert "# fix-it-cron-self-check-host" in post
    assert "# script-contract-fix-it-brain-validation" in post
    assert "# script-contract-shopping-delivery-digest" in post
    assert "# script-contract-family-calendar-reminder-check" in post


# ─── Skip: disabled on fresh install ────────────────────────────────


def test_disabled_agent_entries_skipped_on_fresh_install(tmp_path):
    """With `fix-it` in the disabled file and an empty crontab, every
    fix-it cron is absent after install, and every other cron is
    present.
    """
    bin_dir, state_file = _write_fake_crontab(tmp_path)
    home = _fake_home(tmp_path)
    disabled = home / ".openclaw" / "disabled-agents.txt"
    disabled.write_text("fix-it\n")

    result = _run_installer(bin_dir, home, disabled)
    assert result.returncode == 0, (
        f"installer exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    )

    post = state_file.read_text()
    # Every fix-it-owned entry (DIRECT or CONTRACT) must be absent.
    fix_it_markers = [
        "# fix-it-cron-self-check-host",
        "# script-contract-fix-it-brain-validation",
        "# script-contract-fix-it-conflict-scan",
        "# script-contract-fix-it-file-size-monitor",
        "# script-contract-fix-it-security-audit-alert",
        "# script-contract-fix-it-obsidian-briefing-check",
        "# script-contract-fix-it-workspace-snapshot-check",
        "# script-contract-fix-it-monthly-archival",
        "# script-contract-fix-it-probation-end-reminder",
    ]
    for marker in fix_it_markers:
        assert marker not in post, (
            f"disabled fix-it entry leaked into crontab:\n"
            f"marker: {marker}\ncrontab:\n{post}"
        )

    # Cross-fleet entries whose names don't start with "fix-it-" must
    # still be installed — these orchestrate the whole fleet.
    assert "# morning-fleet-deliver-host" in post
    assert "# fleet-health-host" in post
    assert "# morning-status-host" in post

    # Other agents' entries must still be installed.
    assert "# script-contract-shopping-delivery-digest" in post
    assert "# script-contract-meetings-coach-morning-meeting-brief" in post


# ─── Evict: disabled on re-run over existing crontab ───────────────


def test_existing_disabled_agent_entries_are_evicted(tmp_path):
    """Pre-seed the crontab with fix-it entries, then run the installer
    with `fix-it` in the disabled file. The seeded fix-it lines must be
    evicted in the same sweep that handles STALE_MARKERS / DRIFT_MARKERS.
    """
    bin_dir, state_file = _write_fake_crontab(tmp_path)
    home = _fake_home(tmp_path)
    disabled = home / ".openclaw" / "disabled-agents.txt"
    disabled.write_text("fix-it\n")

    # Seed: a fix-it-brain-validation line that matches what the
    # installer would WANT to install if fix-it were not disabled.
    seed = (
        "0 */6 * * * "
        f"{CONTRACT_WRAPPER} fix-it-brain-validation "
        "/home/openclaw/.openclaw/fix-it-workspace/scripts/brain-validation-check.py "
        "TELEGRAM_BOT_TOKEN 120 # script-contract-fix-it-brain-validation\n"
        "0 0 * * * /home/openclaw/repo/ops/scripts/fix-it-cron-self-check-host.sh "
        "# fix-it-cron-self-check-host\n"
    )
    state_file.write_text(seed)

    result = _run_installer(bin_dir, home, disabled)
    assert result.returncode == 0, (
        f"installer exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    )

    post = state_file.read_text()
    assert "fix-it-brain-validation" not in post, (
        f"seeded fix-it-brain-validation not evicted:\n{post}"
    )
    assert "fix-it-cron-self-check-host" not in post, (
        f"seeded fix-it-cron-self-check-host not evicted:\n{post}"
    )


# ─── File hygiene: comments, blanks, whitespace ─────────────────────


def test_disabled_file_ignores_comments_and_blanks(tmp_path):
    """The disabled file supports `#` comments and blank lines without
    tripping the installer.
    """
    bin_dir, state_file = _write_fake_crontab(tmp_path)
    home = _fake_home(tmp_path)
    disabled = home / ".openclaw" / "disabled-agents.txt"
    disabled.write_text(
        "# disabled agents — operator-managed\n"
        "\n"
        "  fix-it  \n"  # leading/trailing whitespace
        "# trailing comment\n"
        "\n"
    )

    result = _run_installer(bin_dir, home, disabled)
    assert result.returncode == 0, (
        f"installer exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    )

    post = state_file.read_text()
    assert "# script-contract-fix-it-brain-validation" not in post
    assert "# script-contract-shopping-delivery-digest" in post


# ─── Hyphen boundary: "fix-it" does not match "fix-itchy" ───────────


def test_hyphen_boundary_does_not_match_mid_word(tmp_path):
    """A disabled entry `fix-it` must NOT match a hypothetical entry
    called `fix-itchy-something` — the prefix has to be followed by
    `-` or end-of-string, not continue mid-word. Verified against a
    real name we can count on: `fix-it` does not match `fix-it-*`
    entries... wait, it does (and should). We test the tightened case
    by seeding a manual crontab line with an id that begins with
    "fix-it" but extends into a new word without the hyphen boundary,
    and asserting it survives.
    """
    bin_dir, state_file = _write_fake_crontab(tmp_path)
    home = _fake_home(tmp_path)
    disabled = home / ".openclaw" / "disabled-agents.txt"
    disabled.write_text("fix-it\n")

    # Seed a line with marker "# fix-itchy-probe" — it begins with
    # "fix-it" as a character prefix but is NOT bounded by "-" after
    # "fix-it", so it must NOT be treated as a fix-it entry.
    seed = (
        "0 * * * * /usr/bin/true # fix-itchy-probe\n"
    )
    state_file.write_text(seed)

    result = _run_installer(bin_dir, home, disabled)
    assert result.returncode == 0, (
        f"installer exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    )

    post = state_file.read_text()
    assert "fix-itchy-probe" in post, (
        f"'fix-itchy-probe' was accidentally swept by a disabled=fix-it "
        f"entry — hyphen boundary mis-applied:\n{post}"
    )


# ─── Multi-agent: two disabled at once ──────────────────────────────


def test_multiple_disabled_agents(tmp_path):
    """With `fix-it` and `news-digest` both disabled, both fleets' crons
    are absent and every other agent's crons are installed."""
    bin_dir, state_file = _write_fake_crontab(tmp_path)
    home = _fake_home(tmp_path)
    disabled = home / ".openclaw" / "disabled-agents.txt"
    disabled.write_text("fix-it\nnews-digest\n")

    result = _run_installer(bin_dir, home, disabled)
    assert result.returncode == 0, (
        f"installer exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    )

    post = state_file.read_text()
    # Both fleets disabled.
    assert "# script-contract-fix-it-brain-validation" not in post
    assert "# script-contract-news-digest-engagement-poll" not in post
    assert "# news-digest-morning-edition-host" not in post

    # Other agents still present.
    assert "# script-contract-shopping-delivery-digest" in post
    assert "# script-contract-family-calendar-reminder-check" in post
    assert "# script-contract-meetings-coach-morning-meeting-brief" in post
    assert "# script-contract-connector-gmessages-mine" in post
