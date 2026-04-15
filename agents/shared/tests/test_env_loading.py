"""deploy.py must consult ~/clawford/.env for TELEGRAM_CHAT_ID.

On the VPS, secrets live in ~/clawford/.env. A human running
`python3 deploy.py` from a shell that never sourced the env file would
see TELEGRAM_CHAT_ID missing and get a "not in env — crons may fail
delivery" warning every time. That warning has been firing on every
audit run as a false alarm.

Fix: deploy.py provides a load_vps_env() helper that parses
~/clawford/.env into a dict, and the cron-sync path falls back to it
when os.environ is empty.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _import_deploy():
    for mod in list(sys.modules):
        if mod == "deploy" or mod.startswith("deploy."):
            del sys.modules[mod]
    shared_dir = Path(__file__).resolve().parent.parent
    if str(shared_dir) not in sys.path:
        sys.path.insert(0, str(shared_dir))
    import deploy  # type: ignore
    return deploy


def test_load_vps_env_parses_simple_key_value(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_CHAT_ID=111111111\n"
        "EXTRA_KEY=18789\n",
        encoding="utf-8",
    )
    deploy = _import_deploy()
    monkeypatch.setattr(deploy, "VPS_ENV_FILE", env_file)
    env = deploy.load_vps_env()
    assert env["TELEGRAM_CHAT_ID"] == "111111111"
    assert env["EXTRA_KEY"] == "18789"


def test_load_vps_env_ignores_comments_and_blank_lines(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Header comment\n"
        "\n"
        "TELEGRAM_CHAT_ID=42\n"
        "   # indented comment\n"
        "\n",
        encoding="utf-8",
    )
    deploy = _import_deploy()
    monkeypatch.setattr(deploy, "VPS_ENV_FILE", env_file)
    env = deploy.load_vps_env()
    assert env == {"TELEGRAM_CHAT_ID": "42"}


def test_load_vps_env_strips_surrounding_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_CHAT_ID="111111111"\n'
        "OTHER='quoted value'\n",
        encoding="utf-8",
    )
    deploy = _import_deploy()
    monkeypatch.setattr(deploy, "VPS_ENV_FILE", env_file)
    env = deploy.load_vps_env()
    assert env["TELEGRAM_CHAT_ID"] == "111111111"
    assert env["OTHER"] == "quoted value"


def test_load_vps_env_returns_empty_dict_when_file_missing(tmp_path, monkeypatch):
    """Missing file is not an error — returns {}. Callers must be
    prepared to fall back to os.environ."""
    missing = tmp_path / "does-not-exist" / ".env"
    deploy = _import_deploy()
    monkeypatch.setattr(deploy, "VPS_ENV_FILE", missing)
    env = deploy.load_vps_env()
    assert env == {}
