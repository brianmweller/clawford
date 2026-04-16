"""heal_cross_workspace_symlinks — preventive fix for the 2026-04-16
bwrap token regression.

Once P1.2 isolation ships, any symlink in an agent's workspace whose
target is outside that workspace silently breaks inside the bwrap
namespace (the namespace only binds the agent's own workspace).
heal_cross_workspace_symlinks runs at deploy time and replaces
cross-workspace symlinks with independent file copies.

Tests cover:
  - Regular files: untouched
  - Within-workspace symlink: untouched (resolves fine inside namespace)
  - Cross-workspace symlink: replaced with copy, contents preserved,
    restrictive mode preserved
  - Dangling symlink: logged + skipped, not invented
  - Idempotent: running twice is a no-op on the second pass
  - Dry-run: no file mutations
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR))

import deploy  # type: ignore


@pytest.fixture
def two_workspaces(tmp_path: Path):
    """Build two fake agent workspaces with a shared source file that
    tests can symlink into from the other workspace."""
    ws_a = tmp_path / "alpha-workspace"
    ws_b = tmp_path / "beta-workspace"
    ws_a.mkdir()
    ws_b.mkdir()
    # The canonical shared-token file lives inside workspace B (the
    # "home" workspace); workspace A historically symlinked to it.
    source = ws_b / "token.json"
    source.write_text('{"token": "abc123"}', encoding="utf-8")
    os.chmod(source, 0o600)
    return ws_a, ws_b, source


def _manifest_for(workspace: Path) -> SimpleNamespace:
    """Minimal stand-in for deploy.Manifest — heal only reads
    expanded_workspace."""
    return SimpleNamespace(expanded_workspace=workspace)


def test_regular_file_untouched(two_workspaces) -> None:
    ws_a, _, _ = two_workspaces
    (ws_a / "regular.txt").write_text("hello", encoding="utf-8")
    healed = deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))
    assert healed == []
    assert (ws_a / "regular.txt").read_text(encoding="utf-8") == "hello"
    assert not (ws_a / "regular.txt").is_symlink()


def test_within_workspace_symlink_untouched(two_workspaces) -> None:
    """A symlink whose target is inside the same workspace is bwrap-
    safe (the whole workspace is bound RW inside the namespace), so
    don't touch it."""
    ws_a, _, _ = two_workspaces
    (ws_a / "real.txt").write_text("payload", encoding="utf-8")
    (ws_a / "link.txt").symlink_to(ws_a / "real.txt")
    healed = deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))
    assert healed == []
    assert (ws_a / "link.txt").is_symlink()


def test_cross_workspace_symlink_replaced_with_copy(two_workspaces) -> None:
    ws_a, _, source = two_workspaces
    (ws_a / "token.json").symlink_to(source)
    healed = deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))
    assert healed == ["token.json"]
    # Now a real file with the target's contents.
    path = ws_a / "token.json"
    assert not path.is_symlink()
    assert path.read_text(encoding="utf-8") == '{"token": "abc123"}'


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits aren't meaningful on Windows filesystems",
)
def test_cross_workspace_heal_preserves_restrictive_mode(
    two_workspaces,
) -> None:
    """Token files land at 0600 on the Linux VPS; the healed copy
    must inherit that so a copy doesn't accidentally loosen perms."""
    ws_a, _, source = two_workspaces
    (ws_a / "token.json").symlink_to(source)
    deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))
    mode = stat.S_IMODE((ws_a / "token.json").stat().st_mode)
    assert mode == 0o600, f"expected 0o600; got {oct(mode)}"


def test_dangling_cross_workspace_symlink_skipped(
    two_workspaces, capsys: pytest.CaptureFixture,
) -> None:
    """A symlink pointing to a missing target outside the workspace
    should be left alone (we don't invent file contents), but the
    walker must keep going."""
    ws_a, ws_b, _ = two_workspaces
    (ws_a / "dangling.txt").symlink_to(ws_b / "never-existed.txt")
    # Also a valid cross-workspace symlink so we prove the walker
    # survives a dangling one and still heals the next.
    (ws_b / "valid.json").write_text("ok", encoding="utf-8")
    (ws_a / "valid.json").symlink_to(ws_b / "valid.json")

    healed = deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))

    assert healed == ["valid.json"]
    # Dangling symlink left in place (wasn't replaced with empty
    # content).
    assert (ws_a / "dangling.txt").is_symlink()


def test_idempotent_on_second_run(two_workspaces) -> None:
    ws_a, _, source = two_workspaces
    (ws_a / "token.json").symlink_to(source)
    first = deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))
    second = deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))
    assert first == ["token.json"]
    assert second == [], "second pass on the already-healed workspace must be a no-op"


def test_dry_run_does_not_mutate(
    two_workspaces, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy, "_DRY", True)
    ws_a, _, source = two_workspaces
    (ws_a / "token.json").symlink_to(source)
    healed = deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))
    assert healed == [], "dry-run must report but not heal"
    # Symlink still a symlink.
    assert (ws_a / "token.json").is_symlink()


def test_heals_nested_symlinks(two_workspaces) -> None:
    """Files can live anywhere in the workspace tree (e.g.,
    workspace/cache/credentials/token.json); the walker must recurse."""
    ws_a, _, source = two_workspaces
    nested = ws_a / "cache" / "credentials"
    nested.mkdir(parents=True)
    (nested / "token.json").symlink_to(source)
    healed = deploy.heal_cross_workspace_symlinks(_manifest_for(ws_a))
    assert healed == [str(Path("cache") / "credentials" / "token.json")]
    assert not (nested / "token.json").is_symlink()


def test_missing_workspace_returns_empty(tmp_path: Path) -> None:
    """Don't crash when called against an agent that hasn't been
    deployed yet (workspace dir doesn't exist)."""
    missing = tmp_path / "never-deployed"
    healed = deploy.heal_cross_workspace_symlinks(_manifest_for(missing))
    assert healed == []
