"""Tests for scripts/quality-loop.py — stack detection and audit selection.

Covers repo-root discovery, .NET vs Python stack detection, the audit-map
built per stack (script paths, queue files, gate descriptions, prereqs),
and validation of --skip arguments.

Kept as its own module so the maintainability index stays above the radon
gate (the loop driver's own metrics audit scans tests/ too).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import QUALITY_LOOP as ql


# ---------------------------------------------------------------- repo root

def test_git_root(monkeypatch):
    monkeypatch.setattr(ql.subprocess, "check_output", lambda *a, **k: "/tmp/repo\n")
    assert ql.git_root(Path.cwd()) == Path("/tmp/repo")


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, "git"), FileNotFoundError()])
def test_git_root_errors_return_none(monkeypatch, exc):
    monkeypatch.setattr(ql.subprocess, "check_output", lambda *a, **k: (_ for _ in ()).throw(exc))
    assert ql.git_root(Path.cwd()) is None


def test_git_root_or_and_find_repo(monkeypatch):
    monkeypatch.setattr(ql, "git_root", lambda p: Path("/top"))
    assert ql.git_root_or(Path("a"), Path("b")) == Path("/top")
    assert ql.find_repo() == Path("/top")


def test_find_repo_fallback(monkeypatch):
    monkeypatch.setattr(ql, "git_root", lambda p: None)
    assert ql.find_repo() == Path.cwd()


# ------------------------------------------------------------- stack detect

def test_dotnet_marker(tmp_path):
    assert not ql.dotnet_marker(tmp_path)
    (tmp_path / "App.sln").write_text("")
    assert ql.dotnet_marker(tmp_path)


def test_setup_files(tmp_path):
    assert not ql.setup_files(tmp_path)
    (tmp_path / "setup.py").write_text("")
    assert ql.setup_files(tmp_path)
    (tmp_path / "setup.py").unlink()
    (tmp_path / "setup.cfg").write_text("")
    assert ql.setup_files(tmp_path)


def test_python_project_files_and_marker(tmp_path):
    assert not ql.python_marker(tmp_path)
    (tmp_path / "pyproject.toml").write_text("")
    assert ql.python_project_files(tmp_path)
    assert ql.python_marker(tmp_path)
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "requirements.txt").write_text("")
    assert ql.python_marker(tmp_path)


def test_python_or_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ql, "python_marker", lambda repo: True)
    assert ql.python_or_error(tmp_path) == "python"
    monkeypatch.setattr(ql, "python_marker", lambda repo: False)
    with pytest.raises(SystemExit, match="cannot detect the project stack"):
        ql.python_or_error(tmp_path)


def test_detect_stack(tmp_path):
    (tmp_path / "x.sln").write_text("")
    (tmp_path / "pyproject.toml").write_text("")
    assert ql.detect_stack(tmp_path) == "dotnet"  # .NET wins
    (tmp_path / "x.sln").unlink()
    assert ql.detect_stack(tmp_path) == "python"
    (tmp_path / "pyproject.toml").unlink()
    with pytest.raises(SystemExit):
        ql.detect_stack(tmp_path)


# --------------------------------------------------------------- audit map

def test_build_audits_dotnet_order_and_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ql, "REPO", tmp_path)
    monkeypatch.setattr(ql, "LOOP_DIR", tmp_path)
    auds = ql.build_audits("dotnet")
    assert list(auds) == ["quality", "metrics", "warnings", "stryker"]
    assert auds["quality"][0] == tmp_path / "dotnet" / "audit.py"
    assert auds["metrics"][0] == tmp_path / "dotnet" / "metrics-audit.py"
    assert auds["warnings"][0] == tmp_path / "dotnet" / "warnings-audit.py"
    assert auds["stryker"][0] == tmp_path / "dotnet" / "stryker-audit.py"


def test_build_audits_dotnet_prerequisites(tmp_path, monkeypatch):
    monkeypatch.setattr(ql, "REPO", tmp_path)
    monkeypatch.setattr(ql, "LOOP_DIR", tmp_path)
    auds = ql.build_audits("dotnet")
    assert auds["stryker"][3] == ("quality", "metrics", "warnings")
    assert auds["quality"][3] == ()
    assert auds["warnings"][3] == ()


def test_build_audits_python(tmp_path, monkeypatch):
    monkeypatch.setattr(ql, "LOOP_DIR", tmp_path)
    auds = ql.build_audits("python")
    assert list(auds) == ["quality", "metrics", "warnings"]
    assert auds["quality"][0] == tmp_path / "python" / "audit.py"
    assert auds["quality"][1] == "crap-queue.md"
    assert auds["metrics"][2] == "radon rules (MI >= 20, cyclomatic <= 25, args <= 7)"
    assert auds["warnings"][0] == tmp_path / "python" / "warnings-audit.py"
    assert auds["warnings"][1] == "warnings-queue.md"


# ------------------------------------------------------------ skip handling

def test_unknown_skips():
    python_auds = ql.build_audits("python")
    dotnet_auds = ql.build_audits("dotnet")
    assert ql.unknown_skips(["stryker"], python_auds) == ["stryker"]
    assert ql.unknown_skips(["stryker"], dotnet_auds) == []
    assert ql.unknown_skips(["quality", "metrics"], python_auds) == []


def test_validate_skips_unknown_raises():
    with pytest.raises(SystemExit, match="no such audit for a python repo"):
        ql.validate_skips(["bogus"], ql.build_audits("python"), "python")


def test_validate_skips_ok_and_empty():
    ql.validate_skips(["metrics"], ql.build_audits("python"), "python")
    ql.validate_skips([], ql.build_audits("dotnet"), "dotnet")


def test_enabled_audits():
    auds = {"quality": 1, "metrics": 2}
    assert ql.enabled_audits(["quality"], auds) == ["metrics"]
    assert ql.enabled_audits([], auds) == ["quality", "metrics"]


def test_require_enabled():
    assert ql.require_enabled(["quality"]) == ["quality"]
    with pytest.raises(SystemExit, match="all audits skipped"):
        ql.require_enabled([])