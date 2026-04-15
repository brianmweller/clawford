"""Phase 6.5 TDD — native Python security audit.

The pre-6.5 `run_security_audit()` shelled to `openclaw security audit
--deep` and parsed the output. Post-Phase 6 the openclaw CLI is gone,
so the audit has to run in-process. The native audit checks the things
that actually matter post-liberation:

  1. chattr +i on each agent's SOUL.md / IDENTITY.md
  2. File modes on ~/.codex/auth.json and ~/clawford/.env
  3. Brain directory sanity (expected subdirs present)
  4. World-writable walk of the workspaces
  5. lsattr availability (degrades cleanly if missing)

The new entry point is `run_native_audit()` which returns a
{severity: [description]} dict directly — no text parsing.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX mode bits don't round-trip on NTFS — test runs on the Linux VPS",
)


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "agents" / "fix-it" / "scripts" / "security-audit.py"


@pytest.fixture
def security_audit_module():
    """Import security-audit.py as a module. The hyphen in the filename
    means it can't be imported by name, so go through importlib.
    """
    spec = importlib.util.spec_from_file_location(
        "security_audit_fixit", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["security_audit_fixit"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_host_layout(tmp_path: Path):
    """Build a fake host layout that mirrors what the audit walks.

    Returns an object with attributes the test can customize before
    running the audit.
    """
    class Layout:
        pass

    layout = Layout()
    layout.root = tmp_path

    # Two fake agent workspaces under ~/.clawford/
    layout.workspace_root = tmp_path / ".clawford"
    layout.workspace_root.mkdir()
    for agent in ("fix-it", "news-digest"):
        ws = layout.workspace_root / f"{agent}-workspace"
        ws.mkdir()
        (ws / "SOUL.md").write_text("# soul", encoding="utf-8")
        (ws / "IDENTITY.md").write_text("# identity", encoding="utf-8")
    layout.fix_it_soul = layout.workspace_root / "fix-it-workspace" / "SOUL.md"
    layout.news_digest_identity = (
        layout.workspace_root / "news-digest-workspace" / "IDENTITY.md"
    )

    # Secret files
    layout.codex_dir = tmp_path / ".codex"
    layout.codex_dir.mkdir()
    layout.codex_auth = layout.codex_dir / "auth.json"
    layout.codex_auth.write_text("{}", encoding="utf-8")
    layout.codex_auth.chmod(0o600)

    layout.clawford_dir = tmp_path / "openclaw"
    layout.clawford_dir.mkdir()
    layout.env_file = layout.clawford_dir / ".env"
    layout.env_file.write_text("SECRET=x", encoding="utf-8")
    layout.env_file.chmod(0o600)

    # Brain directory with all expected subdirs
    layout.brain_root = tmp_path / "Dropbox" / "openclaw-backup"
    layout.brain_root.mkdir(parents=True)
    for sub in ("agents", "people", "facts", "queues", "commitments"):
        (layout.brain_root / sub).mkdir()

    # Default lsattr runner: every file returns "i" (immutable). Tests
    # override this for specific scenarios.
    layout.lsattr_map = {}  # Path -> attr string ("i" = immutable, "-" = not)

    def lsattr_runner(path: Path) -> str | None:
        # Default: every file is immutable. Tests set layout.lsattr_map
        # entries to override specific paths.
        key = Path(path).resolve()
        return layout.lsattr_map.get(key, "----i----------")

    layout.lsattr_runner = lsattr_runner
    return layout


def _call_native_audit(mod, layout):
    """Call run_native_audit with the fixture overrides."""
    return mod.run_native_audit(
        workspace_root=layout.workspace_root,
        codex_auth_path=layout.codex_auth,
        env_path=layout.env_file,
        brain_path=layout.brain_root,
        lsattr_runner=layout.lsattr_runner,
    )


# ---------------------------------------------------------------------------
# Immutable identity files
# ---------------------------------------------------------------------------


def test_immutable_soul_no_finding(security_audit_module, fake_host_layout):
    """Default: every SOUL.md / IDENTITY.md is chattr +i → no CRITICAL."""
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert findings["CRITICAL"] == [], findings


def test_mutable_soul_emits_critical(security_audit_module, fake_host_layout):
    """SOUL.md without `i` attr → CRITICAL finding mentioning the path."""
    fake_host_layout.lsattr_map[fake_host_layout.fix_it_soul.resolve()] = "---------------"
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    critical = findings["CRITICAL"]
    assert any("SOUL.md" in msg and "fix-it" in msg for msg in critical), critical


def test_mutable_identity_emits_critical(security_audit_module, fake_host_layout):
    """IDENTITY.md without `i` attr → CRITICAL finding."""
    fake_host_layout.lsattr_map[
        fake_host_layout.news_digest_identity.resolve()
    ] = "---------------"
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    critical = findings["CRITICAL"]
    assert any("IDENTITY.md" in msg and "news-digest" in msg for msg in critical), critical


def test_lsattr_missing_emits_low_finding(security_audit_module, fake_host_layout):
    """If lsattr isn't on PATH (runner returns None), audit continues but
    emits a single LOW finding pointing at e2fsprogs."""
    fake_host_layout.lsattr_runner = lambda path: None
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    # Should NOT crash, should NOT produce CRITICAL false positives
    assert findings["CRITICAL"] == [], findings
    assert any("lsattr" in msg or "e2fsprogs" in msg for msg in findings["LOW"]), findings["LOW"]


# ---------------------------------------------------------------------------
# Secret file modes
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_auth_json_mode_0600_no_finding(security_audit_module, fake_host_layout):
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert all("auth.json" not in msg for msg in findings["HIGH"]), findings["HIGH"]


@POSIX_ONLY
def test_auth_json_mode_0644_emits_high(security_audit_module, fake_host_layout):
    fake_host_layout.codex_auth.chmod(0o644)
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert any("auth.json" in msg and "644" in msg for msg in findings["HIGH"]), findings["HIGH"]


def test_auth_json_missing_emits_high(security_audit_module, fake_host_layout):
    fake_host_layout.codex_auth.unlink()
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert any(
        "missing" in msg.lower() and "auth.json" in msg for msg in findings["HIGH"]
    ), findings["HIGH"]


@POSIX_ONLY
def test_env_file_mode_0644_emits_high(security_audit_module, fake_host_layout):
    fake_host_layout.env_file.chmod(0o644)
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert any(".env" in msg and "644" in msg for msg in findings["HIGH"]), findings["HIGH"]


# ---------------------------------------------------------------------------
# Brain directory sanity
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_brain_all_subdirs_present_no_finding(security_audit_module, fake_host_layout):
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert all("brain" not in msg.lower() for msg in findings["MEDIUM"]), findings["MEDIUM"]


def test_brain_missing_subdir_emits_medium(security_audit_module, fake_host_layout):
    import shutil
    shutil.rmtree(fake_host_layout.brain_root / "people")
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert any(
        "brain" in msg.lower() and "people" in msg for msg in findings["MEDIUM"]
    ), findings["MEDIUM"]


def test_brain_directory_missing_emits_medium(security_audit_module, fake_host_layout):
    import shutil
    shutil.rmtree(fake_host_layout.brain_root)
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert any(
        "brain" in msg.lower() and "missing" in msg.lower()
        for msg in findings["MEDIUM"]
    ), findings["MEDIUM"]


# ---------------------------------------------------------------------------
# World-writable walk (POSIX-only — Windows chmod is a no-op for this bit)
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_world_writable_file_emits_medium(security_audit_module, fake_host_layout):
    wfile = (
        fake_host_layout.workspace_root
        / "fix-it-workspace"
        / "oops-world-writable.txt"
    )
    wfile.write_text("sensitive", encoding="utf-8")
    wfile.chmod(0o666)
    findings = _call_native_audit(security_audit_module, fake_host_layout)
    assert any(
        "world-writable" in msg and "oops" in msg for msg in findings["MEDIUM"]
    ), findings["MEDIUM"]


# ---------------------------------------------------------------------------
# get_agent_policies — unchanged contract preserved
# ---------------------------------------------------------------------------


def test_get_agent_policies_reads_exec_approvals(security_audit_module, tmp_path):
    """Legacy exec-approvals reader works when given an explicit path."""
    import json as _json
    approvals = tmp_path / "exec-approvals.json"
    approvals.write_text(
        _json.dumps(
            {
                "defaults": {"security": "full", "ask": "off"},
                "agents": {
                    "main": {"policy": "full", "security": "full"},
                    "fix-it": {"policy": "full", "security": "full"},
                    "shopping": {"policy": "full", "security": "full"},
                },
            }
        ),
        encoding="utf-8",
    )
    policies = security_audit_module.get_agent_policies(approvals_path=approvals)
    assert ("fix-it", "full") in policies
    assert ("shopping", "full") in policies
    assert ("main", "full") in policies


def test_get_agent_policies_handles_missing_file(security_audit_module, tmp_path):
    policies = security_audit_module.get_agent_policies(
        approvals_path=tmp_path / "nope.json"
    )
    assert len(policies) == 1
    assert policies[0][0] == "(error)"


# ---------------------------------------------------------------------------
# Integration: render_report handles native findings dict
# ---------------------------------------------------------------------------


def test_render_report_clean(security_audit_module):
    """Empty findings dict → 'security audit clean' message."""
    findings = {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}
    policies = [("fix-it", "full")]
    report = security_audit_module.render_report(policies, findings)
    assert "Security audit clean" in report
    assert "fix-it" in report


def test_render_report_with_critical_finding(security_audit_module):
    findings = {
        "CRITICAL": ["SOUL.md not immutable — fix-it"],
        "HIGH": [],
        "MEDIUM": [],
        "LOW": [],
    }
    policies = [("fix-it", "full")]
    report = security_audit_module.render_report(policies, findings)
    assert "SOUL.md" in report
    assert "CRITICAL" in report
    assert "Remediation" in report
