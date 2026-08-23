"""Tests for scripts/python/audit.py — repo discovery and coverage input.

Covers git-root discovery, recursive .py file walking (with skipped dirs),
subprocess plumbing for running pytest under coverage, and loading the
coverage.py JSON into the executed/excluded line maps the CRAP math uses.

Split out of the old test_python_audit.py so the module stays above the
radon maintainability gate (the metrics audit scans tests/ too).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.python import audit as pa
from tests.conftest import fake_proc


# ---------------------------------------------------------------- repo root

def test_git_root(monkeypatch):
    monkeypatch.setattr(pa.subprocess, "check_output", lambda *a, **k: "/tmp/repo\n")
    assert pa.git_root(Path.cwd()) == Path("/tmp/repo")


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, "git"), FileNotFoundError()])
def test_git_root_errors_return_none(monkeypatch, exc):
    monkeypatch.setattr(pa.subprocess, "check_output", lambda *a, **k: (_ for _ in ()).throw(exc))
    assert pa.git_root(Path.cwd()) is None


def test_git_root_or_first_wins(monkeypatch):
    monkeypatch.setattr(pa, "git_root", lambda p: Path("/first"))
    assert pa.git_root_or(Path("a"), Path("b")) == Path("/first")


def test_git_root_or_second_fallback(monkeypatch):
    monkeypatch.setattr(pa, "git_root", lambda p: Path("/second") if str(p) == "b" else None)
    assert pa.git_root_or(Path("a"), Path("b")) == Path("/second")


def test_find_repo_prefers_git_toplevel(monkeypatch):
    monkeypatch.setattr(pa, "git_root", lambda p: Path("/top"))
    assert pa.find_repo() == Path("/top")


def test_find_repo_falls_back_to_cwd(monkeypatch):
    monkeypatch.setattr(pa, "git_root", lambda p: None)
    assert pa.find_repo() == Path.cwd()


# ------------------------------------------------------------- file walking

@pytest.mark.parametrize(("path", "expected"), [
    (Path("tests/foo.py"), True),
    (Path("pkg/test/bar.py"), True),
    (Path("src/foo.py"), False),
    (Path("contest.py"), False),
])
def test_is_test_dir(path, expected):
    assert pa.is_test_dir(path) is expected


@pytest.mark.parametrize(("name", "expected"), [
    ("test_foo.py", True),
    ("foo_test.py", True),
    ("foo.py", False),
    ("helpers.pyc", False),
])
def test_is_test_leaf(name, expected):
    assert pa.is_test_leaf(name) is expected


def test_is_test_file():
    assert pa.is_test_file(Path("tests/x.py"))
    assert pa.is_test_file(Path("pkg/test_x.py"))
    assert not pa.is_test_file(Path("pkg/x.py"))


def test_kept_dirs_filters_skipped():
    dirs = [".git", "venv", "__pycache__", "src", "tests", "artifacts"]
    assert pa.kept_dirs(dirs) == ["src", "tests"]


def test_prune_mutates_in_place():
    dirs = ["src", ".pytest_cache", "node_modules"]
    pa.prune(dirs)
    assert dirs == ["src"]


def test_add_py_file_only_python(tmp_path):
    files: list[Path] = []
    pa.add_py_file(files, str(tmp_path), "a.py")
    pa.add_py_file(files, str(tmp_path), "b.txt")
    assert files == [tmp_path / "a.py"]


def test_add_py_files(tmp_path):
    files: list[Path] = []
    pa.add_py_files(files, str(tmp_path), ["x.py", "y.txt", "z.py"])
    assert files == [tmp_path / "x.py", tmp_path / "z.py"]


def test_iter_py_files_walks_skipping_bad_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "skip.py").write_text("")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "a.py").write_text("")
    (tmp_path / "pkg" / "b.py").write_text("")
    (tmp_path / "notes.txt").write_text("")
    got = sorted(pa.iter_py_files())
    assert got == [tmp_path / "a.py", tmp_path / "pkg" / "b.py"]


# ------------------------------------------------- subprocess / coverage io

def test_print_tail(capsys):
    pa.print_tail(fake_proc())
    assert capsys.readouterr().out == "out\nerr\n"


def test_print_tail_silent_when_empty(capsys):
    pa.print_tail(fake_proc(stdout="", stderr=""))
    assert capsys.readouterr().out == ""


def test_ensure_pytest_ok_accepts_zero():
    pa.ensure_pytest_ok(fake_proc(0))


def test_ensure_pytest_ok_raises_on_failure():
    with pytest.raises(SystemExit, match="pytest failed"):
        pa.ensure_pytest_ok(fake_proc(1))


def test_check_pytest_status_warns_no_tests(capsys):
    pa.check_pytest_status(fake_proc(5))
    assert "collected no tests" in capsys.readouterr().out


def test_check_pytest_status_zero_ok(capsys):
    pa.check_pytest_status(fake_proc(0))
    assert capsys.readouterr().out == ""


def test_check_pytest_status_failure_raises():
    with pytest.raises(SystemExit):
        pa.check_pytest_status(fake_proc(1))


def test_handle_missing_coverage_writes_empty(tmp_path, monkeypatch):
    cov = tmp_path / "deep" / "coverage.json"
    monkeypatch.setattr(pa, "COV_JSON", cov)
    pa.handle_missing_coverage()
    assert json.loads(cov.read_text()) == {"files": {}}


def test_handle_missing_coverage_noop_when_present(tmp_path, monkeypatch):
    cov = tmp_path / "coverage.json"
    cov.write_text('{"files": {"a.py": {}}}')
    monkeypatch.setattr(pa, "COV_JSON", cov)
    pa.handle_missing_coverage()
    assert json.loads(cov.read_text()) == {"files": {"a.py": {}}}


def test_dump_coverage_json_runs_coverage(tmp_path, monkeypatch):
    cov = tmp_path / "coverage.json"
    monkeypatch.setattr(pa, "COV_JSON", cov)
    monkeypatch.setattr(pa, "REPO", tmp_path)
    calls = []
    monkeypatch.setattr(pa.subprocess, "run", lambda *a, **k: calls.append((a, k)) or fake_proc(0))
    pa.dump_coverage_json()
    args, kwargs = calls[0]
    assert args[0] == ["coverage", "json", "-o", str(cov)]
    assert kwargs["cwd"] == tmp_path


def test_dump_coverage_json_fallback_on_failure(tmp_path, monkeypatch):
    cov = tmp_path / "coverage.json"
    monkeypatch.setattr(pa, "COV_JSON", cov)
    monkeypatch.setattr(pa.subprocess, "run", lambda *a, **k: fake_proc(1))
    pa.dump_coverage_json()
    assert json.loads(cov.read_text()) == {"files": {}}


def test_run_tests_with_coverage(monkeypatch, capsys):
    dumped = []

    def fake_dump():
        dumped.append(True)

    monkeypatch.setattr(pa, "dump_coverage_json", fake_dump)
    monkeypatch.setattr(pa.subprocess, "run", lambda *a, **k: fake_proc(0, stdout="collected 3", stderr="ok"))
    pa.run_tests_with_coverage()
    assert dumped == [True]
    assert "collected 3" in capsys.readouterr().out


def test_executed_map():
    data = {"files": {
        "a.py": {"executed_lines": [1, 2], "excluded_lines": [3]},
        "b.py": {},
    }}
    got = pa.executed_map(data)
    assert got == {"a.py": ({1, 2}, {3}), "b.py": (set(), set())}


def test_load_executed(tmp_path, monkeypatch):
    cov = tmp_path / "coverage.json"
    cov.write_text('{"files": {"a.py": {"executed_lines": [1]}}}')
    monkeypatch.setattr(pa, "COV_JSON", cov)
    assert pa.load_executed() == {"a.py": ({1}, set())}


def test_load_executed_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "COV_JSON", tmp_path / "nope.json")
    with pytest.raises(SystemExit, match=r"no .*\.json"):
        pa.load_executed()