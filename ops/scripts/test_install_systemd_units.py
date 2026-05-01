"""Tests for ops/scripts/install-systemd-units.py.

Drift-aware deployer for the four clawford systemd units. Two surfaces:

  --check              report drift, exit nonzero if any drift detected
                       (called from the post-merge git hook so a stale
                       unit file shows up immediately after `git pull`)

  --install [--yes]    install: refuse if live differs from repo without
                       --yes; otherwise copy + daemon-reload + restart

Why this exists: 2026-05-01 incident. The repo's costco-socks-tunnel
unit was missing -i in its ExecStart since day one. The live unit on
the VPS had been manually patched at original deploy-time. The
2026-04-29 break-glass refactor's `sudo cp` overwrote the manual fix
with the broken repo version; the autossh process kept running on
its old args until a later restart, then started rejecting auth and
brought down SOCKS for ~half a day.

The fix-fix is to commit the repo to be authoritative AND to surface
the drift (either direction) before clobbering live state.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "scripts" / "install-systemd-units.py"


def _import_module():
    spec = importlib.util.spec_from_file_location("install_systemd_units", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_systemd_units"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Manifest discovery ──────────────────────────────────────────────


def test_manifest_includes_all_four_clawford_units():
    """The four units we know about: tunnel + huckle-push (system),
    calendar-brain + inbox (user). If a fifth gets added to the repo,
    this test fails until the manifest is updated."""
    m = _import_module()
    names = {entry.name for entry in m.UNITS}
    assert names == {
        "costco-socks-tunnel.service",
        "clawford-huckle-push.service",
        "clawford-calendar-brain.service",
        "clawford-inbox.service",
    }


def test_manifest_assigns_scope_correctly():
    m = _import_module()
    by_name = {entry.name: entry for entry in m.UNITS}
    assert by_name["costco-socks-tunnel.service"].scope == "system"
    assert by_name["clawford-huckle-push.service"].scope == "system"
    assert by_name["clawford-calendar-brain.service"].scope == "user"
    assert by_name["clawford-inbox.service"].scope == "user"


def test_manifest_target_paths_match_scope():
    m = _import_module()
    for entry in m.UNITS:
        target = m.target_path(entry, home="/home/openclaw")
        if entry.scope == "system":
            assert target.startswith("/etc/systemd/system/")
        else:
            assert target.startswith("/home/openclaw/.config/systemd/user/")
        assert target.endswith(entry.name)


# ─── Drift detection ─────────────────────────────────────────────────


def test_check_one_unit_reports_in_sync_when_files_match(tmp_path):
    m = _import_module()
    src = tmp_path / "x.service"
    dst = tmp_path / "live-x.service"
    src.write_text("[Service]\nExecStart=/bin/true\n")
    dst.write_text("[Service]\nExecStart=/bin/true\n")
    status = m.check_one(src, dst)
    assert status.state == "in_sync"
    assert status.diff == ""


def test_check_one_unit_reports_drifted_when_files_differ(tmp_path):
    m = _import_module()
    src = tmp_path / "x.service"
    dst = tmp_path / "live-x.service"
    src.write_text("ExecStart=/bin/true -new\n")
    dst.write_text("ExecStart=/bin/true -old\n")
    status = m.check_one(src, dst)
    assert status.state == "drifted"
    assert "-new" in status.diff
    assert "-old" in status.diff


def test_check_one_unit_reports_missing_live_when_dst_absent(tmp_path):
    m = _import_module()
    src = tmp_path / "x.service"
    dst = tmp_path / "nope.service"
    src.write_text("[Service]\n")
    status = m.check_one(src, dst)
    assert status.state == "missing_live"
    assert "not deployed" in status.diff.lower() or "missing" in status.diff.lower()


def test_check_one_unit_reports_missing_repo_when_src_absent(tmp_path):
    """If the repo file is gone but live still exists, that's an
    orphan unit on the VPS — different drift class. Don't pretend
    it's in_sync."""
    m = _import_module()
    src = tmp_path / "nope.service"
    dst = tmp_path / "live.service"
    dst.write_text("[Service]\n")
    status = m.check_one(src, dst)
    assert status.state == "missing_repo"


# ─── Install with confirmation ───────────────────────────────────────


def test_install_one_copies_when_in_sync_is_no_op(tmp_path, monkeypatch):
    """Files identical → install_one returns skipped, no cp/daemon-reload."""
    m = _import_module()
    src = tmp_path / "x.service"
    dst = tmp_path / "live.service"
    src.write_text("[Service]\nExecStart=/bin/true\n")
    dst.write_text("[Service]\nExecStart=/bin/true\n")

    calls = []
    monkeypatch.setattr(m, "_run", lambda cmd, **kw: calls.append(list(cmd)) or MagicMock(returncode=0))

    result = m.install_one(src, dst, scope="system", yes=False)
    assert result.action == "skipped_in_sync"
    assert calls == []


def test_install_one_copies_when_dst_missing(tmp_path, monkeypatch):
    """Missing live file → safe to install without --yes (no manual
    edits to clobber)."""
    m = _import_module()
    src = tmp_path / "x.service"
    src.write_text("[Service]\nExecStart=/bin/true\n")
    dst = tmp_path / "live.service"

    calls = []
    monkeypatch.setattr(m, "_run", lambda cmd, **kw: calls.append(list(cmd)) or MagicMock(returncode=0))

    result = m.install_one(src, dst, scope="system", yes=False)
    assert result.action == "installed"
    assert any("cp" in " ".join(c) for c in calls)


def test_install_one_refuses_drifted_without_yes(tmp_path, monkeypatch):
    """Live file differs from repo, no --yes → REFUSE the copy.
    This is the load-bearing safety. The 2026-05-01 incident was
    exactly this case (manual fix on live, repo broken, cp clobbered)."""
    m = _import_module()
    src = tmp_path / "x.service"
    src.write_text("ExecStart=/bin/repo\n")
    dst = tmp_path / "live.service"
    dst.write_text("ExecStart=/bin/manual-fix\n")

    calls = []
    monkeypatch.setattr(m, "_run", lambda cmd, **kw: calls.append(list(cmd)) or MagicMock(returncode=0))

    result = m.install_one(src, dst, scope="system", yes=False)
    assert result.action == "refused_drifted"
    assert "drift" in result.message.lower() or "differs" in result.message.lower()
    # Live file was NOT touched.
    assert dst.read_text() == "ExecStart=/bin/manual-fix\n"
    # No cp invoked.
    assert not any("cp" in c[0] for c in calls)


def test_install_one_overwrites_drifted_with_yes(tmp_path, monkeypatch):
    m = _import_module()
    src = tmp_path / "x.service"
    src.write_text("ExecStart=/bin/repo\n")
    dst = tmp_path / "live.service"
    dst.write_text("ExecStart=/bin/manual-fix\n")

    calls = []
    monkeypatch.setattr(m, "_run", lambda cmd, **kw: calls.append(list(cmd)) or MagicMock(returncode=0))

    result = m.install_one(src, dst, scope="system", yes=True)
    assert result.action == "installed"
    assert any("cp" in " ".join(c) for c in calls)


def test_install_one_runs_daemon_reload_after_copy(tmp_path, monkeypatch):
    m = _import_module()
    src = tmp_path / "x.service"
    src.write_text("[Service]\n")
    dst = tmp_path / "live.service"

    calls = []
    monkeypatch.setattr(m, "_run", lambda cmd, **kw: calls.append(list(cmd)) or MagicMock(returncode=0))

    m.install_one(src, dst, scope="system", yes=False)
    # Expect a `daemon-reload` invocation after the cp.
    assert any("daemon-reload" in " ".join(c) for c in calls)


def test_install_one_user_scope_uses_user_systemctl(tmp_path, monkeypatch):
    """User-scope units go through `systemctl --user daemon-reload`,
    not the system-scope sudo invocation. Mixing them up silently
    no-ops (the unit you reloaded is the wrong one)."""
    m = _import_module()
    src = tmp_path / "x.service"
    src.write_text("[Service]\n")
    dst = tmp_path / "live.service"

    calls = []
    monkeypatch.setattr(m, "_run", lambda cmd, **kw: calls.append(list(cmd)) or MagicMock(returncode=0))

    m.install_one(src, dst, scope="user", yes=False)
    user_reload = [c for c in calls if "daemon-reload" in " ".join(c)]
    assert user_reload, "must run daemon-reload"
    assert any("--user" in c for c in user_reload), "user scope must pass --user"


# ─── --check mode exit code ──────────────────────────────────────────


def test_check_mode_exits_zero_when_all_in_sync(tmp_path, monkeypatch):
    m = _import_module()
    src_dir = tmp_path / "repo" / "ops" / "systemd"
    src_dir.mkdir(parents=True)
    live_root = tmp_path / "live"
    (live_root / "etc" / "systemd" / "system").mkdir(parents=True)
    (live_root / "home" / "user" / ".config" / "systemd" / "user").mkdir(parents=True)

    # Stub the manifest to a single fake unit
    fake_entry = m.UnitEntry(name="fake.service", scope="system")
    monkeypatch.setattr(m, "UNITS", [fake_entry])
    monkeypatch.setattr(m, "REPO_SYSTEMD_DIR", src_dir)
    monkeypatch.setattr(
        m, "target_path",
        lambda e, home=None: str(live_root / "etc" / "systemd" / "system" / e.name)
        if e.scope == "system"
        else str(live_root / "home" / "user" / ".config" / "systemd" / "user" / e.name)
    )

    (src_dir / "fake.service").write_text("[Service]\nA=1\n")
    (live_root / "etc" / "systemd" / "system" / "fake.service").write_text("[Service]\nA=1\n")

    rc = m.check_main()
    assert rc == 0


def test_check_mode_exits_nonzero_on_drift(tmp_path, monkeypatch):
    m = _import_module()
    src_dir = tmp_path / "repo" / "ops" / "systemd"
    src_dir.mkdir(parents=True)
    live_root = tmp_path / "live"
    (live_root / "etc" / "systemd" / "system").mkdir(parents=True)

    fake_entry = m.UnitEntry(name="fake.service", scope="system")
    monkeypatch.setattr(m, "UNITS", [fake_entry])
    monkeypatch.setattr(m, "REPO_SYSTEMD_DIR", src_dir)
    monkeypatch.setattr(
        m, "target_path",
        lambda e, home=None: str(live_root / "etc" / "systemd" / "system" / e.name)
    )

    (src_dir / "fake.service").write_text("[Service]\nA=1\n")
    (live_root / "etc" / "systemd" / "system" / "fake.service").write_text("[Service]\nA=2\n")

    rc = m.check_main()
    assert rc != 0
