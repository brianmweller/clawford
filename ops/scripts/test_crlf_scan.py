"""Tests for ops/scripts/crlf-scan.py.

Detects CRLF pollution from Windows → Linux SCP transports. Run:
  cd ops/scripts && python3 -m pytest test_crlf_scan.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent


def _load_crlf_scan():
    path = SCRIPTS_DIR / "crlf-scan.py"
    spec = importlib.util.spec_from_file_location("crlf_scan", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["crlf_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def crlf_scan():
    return _load_crlf_scan()


def test_has_crlf_detects_crlf(tmp_path, crlf_scan):
    f = tmp_path / "windows.txt"
    f.write_bytes(b"line one\r\nline two\r\n")
    assert crlf_scan.has_crlf(f) is True


def test_has_crlf_returns_false_for_lf_only(tmp_path, crlf_scan):
    f = tmp_path / "unix.txt"
    f.write_bytes(b"line one\nline two\n")
    assert crlf_scan.has_crlf(f) is False


def test_has_crlf_returns_false_for_empty_file(tmp_path, crlf_scan):
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    assert crlf_scan.has_crlf(f) is False


def test_has_crlf_handles_mixed_bare_cr(tmp_path, crlf_scan):
    """Bare \\r (classic Mac) doesn't count — we only flag CRLF."""
    f = tmp_path / "classic-mac.txt"
    f.write_bytes(b"line one\rline two\r")
    assert crlf_scan.has_crlf(f) is False


def test_normalize_replaces_crlf_with_lf(tmp_path, crlf_scan):
    f = tmp_path / "x.py"
    f.write_bytes(b"import os\r\nimport sys\r\n")
    saved = crlf_scan.normalize(f)
    assert saved == 2  # two CR bytes removed
    assert f.read_bytes() == b"import os\nimport sys\n"


def test_normalize_is_noop_on_lf_file(tmp_path, crlf_scan):
    f = tmp_path / "x.py"
    f.write_bytes(b"import os\nimport sys\n")
    saved = crlf_scan.normalize(f)
    assert saved == 0
    assert f.read_bytes() == b"import os\nimport sys\n"


def test_iter_files_single_file(tmp_path, crlf_scan):
    f = tmp_path / "only.py"
    f.write_bytes(b"x = 1\n")
    found = crlf_scan.iter_files(f, exts=None)
    assert found == [f]


def test_iter_files_single_file_ignores_extension_filter(tmp_path, crlf_scan):
    """Explicit file path wins over --ext filter."""
    f = tmp_path / "only.txt"
    f.write_bytes(b"hi\n")
    found = crlf_scan.iter_files(f, exts=[".py"])
    assert found == [f]


def test_iter_files_dir_filters_by_extension(tmp_path, crlf_scan):
    (tmp_path / "a.py").write_bytes(b"1\n")
    (tmp_path / "b.md").write_bytes(b"2\n")
    (tmp_path / "c.log").write_bytes(b"3\n")
    found = crlf_scan.iter_files(tmp_path, exts=[".py", ".md"])
    names = sorted(p.name for p in found)
    assert names == ["a.py", "b.md"]


def test_iter_files_skips_hidden_dirs(tmp_path, crlf_scan):
    (tmp_path / "a.py").write_bytes(b"1\n")
    git_dir = tmp_path / ".git" / "objects"
    git_dir.mkdir(parents=True)
    (git_dir / "pack.py").write_bytes(b"2\n")
    venv_dir = tmp_path / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "x.py").write_bytes(b"3\n")

    found = crlf_scan.iter_files(tmp_path, exts=[".py"])
    names = sorted(p.name for p in found)
    assert names == ["a.py"]


def test_run_returns_0_when_clean(tmp_path, crlf_scan, capsys):
    f = tmp_path / "clean.py"
    f.write_bytes(b"x = 1\n")
    rc = crlf_scan.run(f, fix=False, exts=None)
    assert rc == 0
    assert "clean" in capsys.readouterr().out


def test_run_returns_1_when_crlf_found_and_not_fixed(tmp_path, crlf_scan, capsys):
    f = tmp_path / "dirty.py"
    f.write_bytes(b"x = 1\r\n")
    rc = crlf_scan.run(f, fix=False, exts=None)
    assert rc == 1
    captured = capsys.readouterr()
    assert "CRLF" in captured.out
    assert "rerun with --fix" in captured.err
    # File is untouched when --fix is absent.
    assert f.read_bytes() == b"x = 1\r\n"


def test_run_fix_flag_normalizes_and_returns_0(tmp_path, crlf_scan, capsys):
    f = tmp_path / "dirty.py"
    f.write_bytes(b"x = 1\r\ny = 2\r\n")
    rc = crlf_scan.run(f, fix=True, exts=None)
    assert rc == 0
    assert f.read_bytes() == b"x = 1\ny = 2\n"
    assert "fixed" in capsys.readouterr().out


def test_run_returns_2_on_missing_path(tmp_path, crlf_scan, capsys):
    rc = crlf_scan.run(tmp_path / "does-not-exist.py", fix=False, exts=None)
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_run_dir_scan_finds_only_crlf_files(tmp_path, crlf_scan, capsys):
    (tmp_path / "clean.py").write_bytes(b"x = 1\n")
    (tmp_path / "dirty.py").write_bytes(b"y = 2\r\n")
    (tmp_path / "also-dirty.md").write_bytes(b"hello\r\n")
    rc = crlf_scan.run(tmp_path, fix=False, exts=None)
    assert rc == 1
    out = capsys.readouterr().out
    assert "dirty.py" in out
    assert "also-dirty.md" in out
    assert "clean.py" not in out
