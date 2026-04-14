"""Safeguard 10: config-file source resolution and bootstrap.

After the 2026-04-13 PII sanitization (commit 3eb1b84), real agent config
files (SOUL.md, IDENTITY.md, TOOLS.md, etc.) are gitignored — only the
.example templates are committed. deploy.py must refuse to deploy when
the real files are missing or still carry the bootstrap sentinel that
--bootstrap-configs prepends to unedited scaffolds.

Core contract: missing real file + .example sibling present
  → error with --bootstrap-configs hint, exit 5.
Missing real file + NO .example sibling
  → error "truly broken manifest", exit 5.
Sentinel-bearing file present
  → error naming sentinel files, exit 5.
All real files present, no sentinels
  → deploy proceeds.
--skip-files bypasses the check entirely (sync_files is never called).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


SENTINEL = "CLAWFORD_BOOTSTRAP_UNEDITED"


def _make_args(agent_id="testagent", skip_files=False, allow_dirty=True):
    """Default args for config-resolution tests.

    allow_dirty=True bypasses Safeguard 2 because tests mutate the fake
    source repo in the test body (delete SOUL.md, create SOUL.md.example)
    which dirties the git state.
    """
    return argparse.Namespace(
        agent_id=agent_id,
        all=False,
        exclude=[],
        dry_run=False,
        skip_files=skip_files,
        skip_scripts=False,
        skip_crons=True,
        skip_channel=True,
        remove_orphans=False,
        allow_dirty=allow_dirty,
    )


# ────────────────────────────────────────────────────────────────────────
# Safeguard 10 gating
# ────────────────────────────────────────────────────────────────────────


def test_safeguard_10_passes_when_real_files_present(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path
):
    """Baseline: fixture has real SOUL.md and TOOLS.md, deploy runs the
    copy loop and lands files in the workspace."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)

    rc = deploy_module.deploy_one("testagent", _make_args())

    assert rc == 0
    assert (fake_workspace / "SOUL.md").exists()
    assert (fake_workspace / "TOOLS.md").exists()


def test_safeguard_10_blocks_deploy_when_real_file_missing_has_template(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys
):
    """Real SOUL.md missing, SOUL.md.example present — must fail with exit 5
    and point the operator at --bootstrap-configs."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    agent_dir = fake_source_repo / "agents" / "testagent"
    (agent_dir / "SOUL.md").unlink()
    (agent_dir / "SOUL.md.example").write_text(
        "# Template SOUL\nDummy values.\n", encoding="utf-8"
    )

    rc = deploy_module.deploy_one("testagent", _make_args())

    assert rc == 5
    captured = capsys.readouterr().out
    assert "SOUL.md" in captured
    assert "--bootstrap-configs" in captured
    # Workspace must be untouched — Safeguard 10 runs inside sync_files
    # before any copy happens.
    assert not (fake_workspace / "SOUL.md").exists()


def test_safeguard_10_blocks_deploy_when_real_file_missing_no_template(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys
):
    """Real SOUL.md missing and NO .example sibling — distinct error
    message from the bootstrap-needed case."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    (fake_source_repo / "agents" / "testagent" / "SOUL.md").unlink()

    rc = deploy_module.deploy_one("testagent", _make_args())

    assert rc == 5
    captured = capsys.readouterr().out
    assert "SOUL.md" in captured
    # Distinct message — does NOT point at bootstrap, because there's no
    # template to bootstrap from. This is a broken-manifest case.
    assert "no .example" in captured.lower() or "no template" in captured.lower()


def test_safeguard_10_rejects_unedited_bootstrap_sentinel(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path, capsys
):
    """Real SOUL.md present but first line carries the sentinel — reject.
    This is the defense against 'the operator ran --bootstrap-configs but forgot
    to hand-edit, and fake PII silently shipped to the workspace'."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    soul = fake_source_repo / "agents" / "testagent" / "SOUL.md"
    original = soul.read_text(encoding="utf-8")
    soul.write_text(
        f"<!-- {SENTINEL}: replace dummy values and delete this line -->\n\n{original}",
        encoding="utf-8",
    )

    rc = deploy_module.deploy_one("testagent", _make_args())

    assert rc == 5
    captured = capsys.readouterr().out
    assert "SOUL.md" in captured
    assert SENTINEL in captured


def test_safeguard_10_accepts_file_after_sentinel_stripped(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path
):
    """Once the sentinel line is gone, deploy proceeds normally."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    soul = fake_source_repo / "agents" / "testagent" / "SOUL.md"
    # Write real content without a sentinel — same as fixture default,
    # but explicitly tests the "sentinel-free after edit" path.
    soul.write_text("# testagent soul — real values\nBrian was here\n", encoding="utf-8")

    rc = deploy_module.deploy_one("testagent", _make_args())

    assert rc == 0
    assert "the operator was here" in (fake_workspace / "SOUL.md").read_text(encoding="utf-8")


def test_skip_files_bypasses_safeguard_10(
    deploy_module, fake_source_repo, fake_workspace, monkeypatch, tmp_path
):
    """--skip-files must still work when config files would fail Safeguard 10.
    the operator needs to iterate on crons/scripts/approvals while a bootstrap is
    still in progress."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    agent_dir = fake_source_repo / "agents" / "testagent"
    (agent_dir / "SOUL.md").unlink()
    (agent_dir / "SOUL.md.example").write_text("# Template\n", encoding="utf-8")

    rc = deploy_module.deploy_one("testagent", _make_args(skip_files=True))

    assert rc == 0, f"skip_files=True should bypass Safeguard 10; got rc={rc}"
    # sync_files was skipped, so no SOUL.md in the workspace
    assert not (fake_workspace / "SOUL.md").exists()


# ────────────────────────────────────────────────────────────────────────
# bootstrap_configs() scaffolding
# ────────────────────────────────────────────────────────────────────────


def test_bootstrap_configs_scaffolds_md_with_sentinel(
    deploy_module, fake_source_repo, monkeypatch, tmp_path
):
    """--bootstrap-configs copies SOUL.md.example → SOUL.md with the
    sentinel prepended. the operator must edit+delete the sentinel before deploy."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    agent_dir = fake_source_repo / "agents" / "testagent"
    (agent_dir / "SOUL.md").unlink()
    template_content = "# Template SOUL\nBody line one.\nBody line two.\n"
    (agent_dir / "SOUL.md.example").write_text(template_content, encoding="utf-8")

    rc = deploy_module.bootstrap_configs("testagent")

    assert rc == 0
    real = agent_dir / "SOUL.md"
    assert real.exists()
    content = real.read_text(encoding="utf-8")
    assert SENTINEL in content.splitlines()[0], \
        f"first line must contain sentinel, got: {content.splitlines()[0]!r}"
    # Template body survives after the sentinel.
    assert "Body line one." in content
    assert "Body line two." in content


def test_bootstrap_configs_copies_json_verbatim(
    deploy_module, fake_source_repo, monkeypatch, tmp_path
):
    """JSON files must be copied without a sentinel — prepending a comment
    would break JSON parsing."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    agent_dir = fake_source_repo / "agents" / "testagent"
    template_payload = {"nested": {"key": "value"}, "list": [1, 2, 3]}
    (agent_dir / "widget-config.json.example").write_text(
        json.dumps(template_payload, indent=2), encoding="utf-8"
    )

    rc = deploy_module.bootstrap_configs("testagent")

    assert rc == 0
    real = agent_dir / "widget-config.json"
    assert real.exists()
    # Must parse as valid JSON and match the template payload byte-for-byte.
    loaded = json.loads(real.read_text(encoding="utf-8"))
    assert loaded == template_payload
    assert SENTINEL not in real.read_text(encoding="utf-8")


def test_bootstrap_configs_is_idempotent(
    deploy_module, fake_source_repo, monkeypatch, tmp_path, capsys
):
    """Running bootstrap twice must not clobber hand-edits made between runs."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    agent_dir = fake_source_repo / "agents" / "testagent"
    (agent_dir / "SOUL.md").unlink()
    (agent_dir / "SOUL.md.example").write_text("# Template\nDummy.\n", encoding="utf-8")

    rc1 = deploy_module.bootstrap_configs("testagent")
    assert rc1 == 0
    real = agent_dir / "SOUL.md"
    assert real.exists()

    # Hand-edit: strip sentinel, add real content.
    hand_edited = "# Real soul\nBrian was here — real values only.\n"
    real.write_text(hand_edited, encoding="utf-8")

    rc2 = deploy_module.bootstrap_configs("testagent")

    assert rc2 == 0
    assert real.read_text(encoding="utf-8") == hand_edited, \
        "second bootstrap must not overwrite hand-edited content"
    captured = capsys.readouterr().out
    assert "already present" in captured.lower() or "skip" in captured.lower()


def test_bootstrap_configs_walks_nested_example_files(
    deploy_module, fake_source_repo, monkeypatch, tmp_path
):
    """Bootstrap globs recursively so nested templates like
    agents/connector/scripts/mine/family_map.py.example are handled,
    creating parent directories as needed."""
    monkeypatch.setattr(deploy_module, "BACKUPS_ROOT", tmp_path / "backups", raising=False)
    agent_dir = fake_source_repo / "agents" / "testagent"
    nested_dir = agent_dir / "scripts" / "mine"
    nested_dir.mkdir(parents=True)
    (nested_dir / "family_map.py.example").write_text(
        "MAP = {'example': 'value'}\n", encoding="utf-8"
    )

    rc = deploy_module.bootstrap_configs("testagent")

    assert rc == 0
    real = nested_dir / "family_map.py"
    assert real.exists()
    # .py files get no sentinel (Python syntax would break).
    content = real.read_text(encoding="utf-8")
    assert SENTINEL not in content
    assert "MAP = {'example': 'value'}" in content
