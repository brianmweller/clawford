"""Tests for Mr Fixit's three new write-capable tools and the
_cron_lookup helper that backs propose_rerun_cron.

Tools under test:
  - _cron_lookup.lookup_cron / list_host_crons / list_llm_crons
  - tools.propose_snooze_alert / confirm_snooze_alert
  - tools.propose_refresh_session / confirm_refresh_session
  - tools.propose_rerun_cron / confirm_rerun_cron

Pattern mirrored from agents/shared/tests/test_pending_actions.py:
  - CLAWFORD_WORKSPACE_ROOT env points pending-actions.json into tmp_path
  - tools module is imported fresh per-test so monkeypatched paths apply
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
FIX_IT_DIR = REPO_ROOT / "agents" / "fix-it"
SHARED_DIR = REPO_ROOT / "agents" / "shared"


@pytest.fixture
def fixit_paths_clean():
    """Make sure each test gets a fresh import of fix-it's modules so
    monkeypatched module-level constants don't leak between tests."""
    for mod_name in ("tools", "_cron_lookup", "pending_actions", "memory_writer"):
        sys.modules.pop(mod_name, None)
    yield
    for mod_name in ("tools", "_cron_lookup", "pending_actions", "memory_writer"):
        sys.modules.pop(mod_name, None)


@pytest.fixture
def import_paths():
    """Add fix-it/ and shared/ to sys.path so `import tools`,
    `import _cron_lookup`, and `import pending_actions` resolve."""
    added = []
    for p in (FIX_IT_DIR, SHARED_DIR):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)
            added.append(sp)
    yield
    for sp in added:
        try:
            sys.path.remove(sp)
        except ValueError:
            pass


@pytest.fixture
def install_script_fixture(tmp_path):
    """Minimal install-host-cron.sh with one DIRECT and a few CONTRACT entries."""
    snippet = tmp_path / "install-host-cron.sh"
    snippet.write_text(
        'DIRECT_ENTRIES=(\n'
        '  "*/5 * * * *|costco-token-refresh-host.sh|# costco-token-refresh-host"\n'
        '  "0 12 * * *|morning-fleet-deliver-host.sh|# morning-fleet-deliver-host"\n'
        ')\n'
        '\n'
        'CONTRACT_ENTRIES=(\n'
        '  "0 */6 * * *|fix-it-brain-validation|/path/brain-validation-check.py|TELEGRAM_BOT_TOKEN|120"\n'
        '  "0 */2 * * *|fix-it-conflict-scan|/path/conflict-scan.py|TELEGRAM_BOT_TOKEN|120"\n'
        ')\n',
        encoding="utf-8",
    )
    return snippet


@pytest.fixture
def llm_crons_fixture(tmp_path):
    """A fake expected-crons.json so list_llm_crons has something to read."""
    agent_dir = tmp_path / "agents" / "meetings-coach"
    agent_dir.mkdir(parents=True)
    (agent_dir / "expected-crons.json").write_text(
        json.dumps({
            "meetings-coach": {
                "weekly-review": {"cron": "0 0 * * 5", "message": "review the week"},
            }
        }),
        encoding="utf-8",
    )
    return tmp_path / "agents"


# ───────────────────────── _cron_lookup ─────────────────────────

class TestCronLookup:
    def test_list_host_crons_includes_direct_and_contract(
        self, fixit_paths_clean, import_paths, install_script_fixture, monkeypatch
    ):
        import _cron_lookup as cl
        monkeypatch.setattr(cl, "INSTALL_SCRIPT", install_script_fixture)

        crons = cl.list_host_crons()
        names = [c["name"] for c in crons]
        assert "costco-token-refresh-host" in names
        assert "morning-fleet-deliver-host" in names
        assert "fix-it-brain-validation" in names
        assert "fix-it-conflict-scan" in names

    def test_list_host_crons_records_kind_and_schedule(
        self, fixit_paths_clean, import_paths, install_script_fixture, monkeypatch
    ):
        import _cron_lookup as cl
        monkeypatch.setattr(cl, "INSTALL_SCRIPT", install_script_fixture)

        crons = {c["name"]: c for c in cl.list_host_crons()}
        direct = crons["morning-fleet-deliver-host"]
        assert direct["kind"] == "host"
        assert direct["contract"] is False
        assert direct["schedule"] == "0 12 * * *"
        assert str(direct["wrapper_path"]).endswith("morning-fleet-deliver-host.sh")

        contract = crons["fix-it-conflict-scan"]
        assert contract["kind"] == "host"
        assert contract["contract"] is True
        assert contract["schedule"] == "0 */2 * * *"
        assert contract["script_path"] == "/path/conflict-scan.py"
        assert contract["token_env"] == "TELEGRAM_BOT_TOKEN"
        assert contract["timeout_s"] == "120"

    def test_list_llm_crons_walks_expected_crons_files(
        self, fixit_paths_clean, import_paths, llm_crons_fixture, monkeypatch
    ):
        import _cron_lookup as cl
        monkeypatch.setattr(cl, "AGENTS_DIR", llm_crons_fixture)

        crons = cl.list_llm_crons()
        names = [c["name"] for c in crons]
        assert "weekly-review" in names
        wr = next(c for c in crons if c["name"] == "weekly-review")
        assert wr["kind"] == "llm"
        assert wr["agent"] == "meetings-coach"
        assert wr["schedule"] == "0 0 * * 5"

    def test_lookup_cron_finds_host(
        self, fixit_paths_clean, import_paths, install_script_fixture, monkeypatch
    ):
        import _cron_lookup as cl
        monkeypatch.setattr(cl, "INSTALL_SCRIPT", install_script_fixture)
        monkeypatch.setattr(cl, "AGENTS_DIR", Path("/nonexistent"))

        result = cl.lookup_cron("fix-it-conflict-scan")
        assert result is not None
        assert result["kind"] == "host"
        assert result["contract"] is True

    def test_lookup_cron_unknown_returns_none(
        self, fixit_paths_clean, import_paths, install_script_fixture, monkeypatch
    ):
        import _cron_lookup as cl
        monkeypatch.setattr(cl, "INSTALL_SCRIPT", install_script_fixture)
        monkeypatch.setattr(cl, "AGENTS_DIR", Path("/nonexistent"))

        assert cl.lookup_cron("not-a-real-cron") is None


# ───────────────────────── propose/confirm_snooze_alert ─────────────────────────


@pytest.fixture
def snooze_env(tmp_path, monkeypatch, fixit_paths_clean, import_paths):
    """Workspace + KNOWN_ISSUES.md path patched; tools module fresh-loaded."""
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "fix-it-workspace").mkdir()

    brain = tmp_path / "openclaw-backup" / "fix-it"
    brain.mkdir(parents=True)
    known_issues = brain / "KNOWN_ISSUES.md"
    known_issues.write_text("", encoding="utf-8")

    import tools
    monkeypatch.setattr(tools, "KNOWN_ISSUES_PATH", str(known_issues))
    return tools, known_issues


class TestSnoozeAlert:
    def test_invalid_regex_rejected(self, snooze_env):
        tools, _ = snooze_env
        result = tools.propose_snooze_alert(pattern="(", hours=1, reason="x")
        assert "__pending_action__" not in result
        assert result["status"] == "error"
        assert "regex" in result["error"].lower()

    def test_overbroad_pattern_rejected(self, snooze_env):
        tools, _ = snooze_env
        result = tools.propose_snooze_alert(pattern=".*", hours=1, reason="x")
        assert result["status"] == "error"

    def test_too_short_pattern_rejected(self, snooze_env):
        tools, _ = snooze_env
        result = tools.propose_snooze_alert(pattern="ab", hours=1, reason="x")
        assert result["status"] == "error"

    def test_cap_168_hours(self, snooze_env):
        tools, _ = snooze_env
        result = tools.propose_snooze_alert(pattern="heartbeat", hours=200, reason="x")
        assert result["status"] == "error"
        assert "168" in result["error"]

    def test_propose_stages_with_short_ttl(self, snooze_env):
        tools, _ = snooze_env
        result = tools.propose_snooze_alert(
            pattern="heartbeat stale", hours=4, reason="testing"
        )
        assert "__pending_action__" in result
        assert result["expires_in"].startswith("1")

    def test_confirm_appends_parseable_block(self, snooze_env):
        tools, known_issues = snooze_env
        # Directly invoke confirm executor (callback-only path).
        result = tools.confirm_snooze_alert(
            pattern="heartbeat stale", hours=24, reason="overnight known flake"
        )
        assert result["status"] == "ok"

        # Round-trip through morning-status's parser.
        ms_path = FIX_IT_DIR / "scripts" / "morning-status.py"
        spec = importlib.util.spec_from_file_location("ms_for_test", ms_path)
        ms = importlib.util.module_from_spec(spec)
        sys.modules["ms_for_test"] = ms  # dataclass needs the module registered
        spec.loader.exec_module(ms)

        text = known_issues.read_text(encoding="utf-8")
        issues = ms.parse_known_issues(text)
        assert len(issues) == 1
        assert issues[0].pattern == "heartbeat stale"
        assert issues[0].reason == "overnight known flake"

    def test_expires_uses_now_at_confirm_time(self, snooze_env):
        tools, known_issues = snooze_env
        # Freeze time to 2026-04-15 12:00 UTC at confirm.
        frozen_now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)

        class _FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_now if tz is None else frozen_now.astimezone(tz)

        with patch.object(tools, "datetime", _FakeDT):
            tools.confirm_snooze_alert(
                pattern="heartbeat stale", hours=48, reason="test"
            )

        text = known_issues.read_text(encoding="utf-8")
        # 12 + 48h = 2026-04-17
        assert "2026-04-17" in text


# ───────────────────────── propose/confirm_refresh_session ─────────────────────────


@pytest.fixture
def refresh_env(tmp_path, monkeypatch, fixit_paths_clean, import_paths):
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "fix-it-workspace").mkdir()
    import tools
    return tools


class TestRefreshSession:
    def test_unknown_source_rejected(self, refresh_env):
        tools = refresh_env
        result = tools.propose_refresh_session(source="amazon")
        assert "__pending_action__" not in result
        assert result["status"] == "error"

    def test_costco_propose_stages(self, refresh_env):
        tools = refresh_env
        result = tools.propose_refresh_session(source="costco")
        assert "__pending_action__" in result
        assert "costco" in result["summary"].lower()

    def test_google_propose_stages(self, refresh_env):
        tools = refresh_env
        result = tools.propose_refresh_session(source="google")
        assert "__pending_action__" in result

    def test_costco_confirm_subprocesses_headless_script(self, refresh_env, monkeypatch):
        tools = refresh_env

        recorded = {}

        def _fake_run(argv, **kwargs):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs
            class R:
                returncode = 0
                stdout = "ok"
                stderr = ""
            return R()

        monkeypatch.setattr(tools.subprocess, "run", _fake_run)

        result = tools.confirm_refresh_session(source="costco")
        assert result["status"] == "ok"
        # argv[0] is python3 or similar; the script must be in argv.
        joined = " ".join(str(a) for a in recorded["argv"])
        assert "costco_refresh_headless" in joined

    def test_google_confirm_walks_both_tokens(self, refresh_env, monkeypatch):
        tools = refresh_env

        called_with = []

        def _fake_refresh(token_path, max_age_days=30, scopes=None):
            called_with.append(token_path)
            return True

        monkeypatch.setattr(tools.google_oauth, "refresh_if_stale", _fake_refresh)

        result = tools.confirm_refresh_session(source="google")
        assert result["status"] == "ok"
        joined = " ".join(called_with)
        assert "family-calendar" in joined
        assert "meetings-coach" in joined


# ───────────────────────── propose/confirm_rerun_cron ─────────────────────────


@pytest.fixture
def rerun_env(tmp_path, monkeypatch, fixit_paths_clean, import_paths, install_script_fixture):
    monkeypatch.setenv("CLAWFORD_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "fix-it-workspace").mkdir()
    import _cron_lookup as cl
    monkeypatch.setattr(cl, "INSTALL_SCRIPT", install_script_fixture)
    monkeypatch.setattr(cl, "AGENTS_DIR", Path("/nonexistent"))
    import tools
    return tools


class TestRerunCron:
    def test_unknown_cron_returns_error(self, rerun_env):
        tools = rerun_env
        result = tools.propose_rerun_cron(name="not-a-cron", reason="testing")
        assert "__pending_action__" not in result
        assert result["status"] == "error"

    def test_propose_summary_carries_warning(self, rerun_env):
        tools = rerun_env
        result = tools.propose_rerun_cron(
            name="fix-it-conflict-scan", reason="re-test conflict scan"
        )
        assert "__pending_action__" in result
        s = result["summary"]
        assert "WARNING" in s.upper() or "double-execute" in s.lower() or "may double" in s.lower()

    def test_confirm_host_direct_invokes_wrapper(self, rerun_env, monkeypatch):
        tools = rerun_env
        recorded = {}

        def _fake_run(argv, **kwargs):
            recorded["argv"] = list(argv)
            class R:
                returncode = 0
                stdout = "ran"
                stderr = ""
            return R()

        monkeypatch.setattr(tools.subprocess, "run", _fake_run)
        result = tools.confirm_rerun_cron(
            name="morning-fleet-deliver-host",
            kind="host",
            contract=False,
            wrapper_path="/repo/ops/scripts/morning-fleet-deliver-host.sh",
            schedule="0 12 * * *",
            reason="test",
        )
        assert result["status"] == "ok"
        assert recorded["argv"][0] == "/repo/ops/scripts/morning-fleet-deliver-host.sh"

    def test_confirm_host_contract_invokes_wrapper_with_args(self, rerun_env, monkeypatch):
        tools = rerun_env
        recorded = {}

        def _fake_run(argv, **kwargs):
            recorded["argv"] = list(argv)
            class R:
                returncode = 0
                stdout = "ran"
                stderr = ""
            return R()

        monkeypatch.setattr(tools.subprocess, "run", _fake_run)
        result = tools.confirm_rerun_cron(
            name="fix-it-conflict-scan",
            kind="host",
            contract=True,
            wrapper_path="/repo/ops/scripts/script-contract-host.sh",
            schedule="0 */2 * * *",
            reason="test",
            script_path="/path/conflict-scan.py",
            token_env="TELEGRAM_BOT_TOKEN",
            timeout_s="120",
        )
        assert result["status"] == "ok"
        # Contract wrapper expects: <wrapper> <logname> <script_path> <token_env> <timeout>
        assert recorded["argv"][0].endswith("script-contract-host.sh")
        assert "fix-it-conflict-scan" in recorded["argv"]
        assert "/path/conflict-scan.py" in recorded["argv"]
        assert "TELEGRAM_BOT_TOKEN" in recorded["argv"]

    def test_confirm_llm_kind_returns_error(self, rerun_env):
        tools = rerun_env
        result = tools.confirm_rerun_cron(
            name="weekly-review",
            kind="llm",
            contract=False,
            wrapper_path="",
            schedule="0 0 * * 5",
            reason="test",
        )
        assert result["status"] == "error"
        assert "retired" in result["error"].lower() or "no runner" in result["error"].lower()


# ───────────────────────── TOOLS manifest exposure ─────────────────────────


def test_tools_manifest_exposes_three_propose_tools(fixit_paths_clean, import_paths):
    import tools
    names = {t["name"] for t in tools.TOOLS}
    assert "propose_rerun_cron" in names
    assert "propose_snooze_alert" in names
    assert "propose_refresh_session" in names


def test_confirm_executors_not_in_manifest_but_in_executors(fixit_paths_clean, import_paths):
    import tools
    names = {t["name"] for t in tools.TOOLS}
    for confirm_name in ("confirm_rerun_cron", "confirm_snooze_alert", "confirm_refresh_session"):
        assert confirm_name not in names, f"{confirm_name} must not appear in TOOLS manifest"
        assert confirm_name in tools.EXECUTORS, f"{confirm_name} missing from EXECUTORS dict"
