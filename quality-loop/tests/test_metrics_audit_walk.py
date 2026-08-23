"""Tests for scripts/python/metrics-audit.py — repo walking and arg parsing.

Covers git-root discovery, .py file walking (skipped dirs), the argument
count logic (self/cls dropping, posonly/kwonly counting, *args/**kw
exclusion), and source reading / ast parsing helpers.

Split out of the old test_metrics_audit.py so the module stays above the
radon maintainability gate (the metrics audit scans tests/ too).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import METRICS_AUDIT as ma


# ---------------------------------------------------------------- repo root

def test_git_root(monkeypatch):
    monkeypatch.setattr(ma.subprocess, "check_output", lambda *a, **k: "/tmp/repo\n")
    assert ma.git_root(Path.cwd()) == Path("/tmp/repo")


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, "git"), FileNotFoundError()])
def test_git_root_errors_return_none(monkeypatch, exc):
    monkeypatch.setattr(ma.subprocess, "check_output", lambda *a, **k: (_ for _ in ()).throw(exc))
    assert ma.git_root(Path.cwd()) is None


def test_git_root_or_order(monkeypatch):
    monkeypatch.setattr(ma, "git_root", lambda p: Path("/first"))
    assert ma.git_root_or(Path("a"), Path("b")) == Path("/first")


def test_find_repo(monkeypatch):
    monkeypatch.setattr(ma, "git_root", lambda p: Path("/top"))
    assert ma.find_repo() == Path("/top")


def test_find_repo_fallback(monkeypatch):
    monkeypatch.setattr(ma, "git_root", lambda p: None)
    assert ma.find_repo() == Path.cwd()


# ------------------------------------------------------------- file walking

def test_kept_dirs_filters_skipped():
    assert ma.kept_dirs([".venv", "src", "build", ".git"]) == ["src"]


def test_prune_mutates_in_place():
    dirs = ["site-packages", "pkg", "dist"]
    ma.prune(dirs)
    assert dirs == ["pkg"]


def test_add_py_file_and_files(tmp_path):
    files: list[Path] = []
    ma.add_py_files(files, str(tmp_path), ["a.py", "b.txt", "c.py"])
    assert files == [tmp_path / "a.py", tmp_path / "c.py"]
    ma.add_py_file(files, str(tmp_path), "d.py")
    assert files[-1] == tmp_path / "d.py"


def test_iter_py_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "REPO", tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "a.py").write_text("")
    (tmp_path / "pkg" / "b.py").write_text("")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("")
    (tmp_path / "readme.md").write_text("")
    assert sorted(ma.iter_py_files()) == [tmp_path / "a.py", tmp_path / "pkg" / "b.py"]


# -------------------------------------------------------------- args / rules

def test_method_params():
    args = SimpleNamespace(args=[SimpleNamespace(arg="self"), SimpleNamespace(arg="x")])
    assert ma.method_params(args.args)
    assert not ma.method_params([SimpleNamespace(arg="x")])


def test_drop_self():
    args = [SimpleNamespace(arg="self"), SimpleNamespace(arg="a"), SimpleNamespace(arg="b")]
    assert [a.arg for a in ma.drop_self(args, args)] == ["a", "b"]
    plain = [SimpleNamespace(arg="a"), SimpleNamespace(arg="b")]
    assert ma.drop_self(plain, plain) == plain


def test_arg_count_counts_posonly_kwonly_excludes_varargs():
    src = "def f(self, a, b=1, *args, c, d, **kw):\n    pass\n"
    tree = __import__("ast").parse(src)
    fn = tree.body[0]
    assert ma.arg_count(fn.args) == 4  # a, b, c, d (self dropped, *args/**kw excluded)
    src2 = "def g(a, /, b):\n    pass\n"
    fn2 = __import__("ast").parse(src2).body[0]
    assert ma.arg_count(fn2.args) == 2


def test_collect_args_and_function_args():
    src = "def f(self, a):\n    pass\n\nasync def g(x, y, z, w, v, u, t):\n    pass\n"
    tree = __import__("ast").parse(src)
    counts = ma.function_args(tree)
    assert counts == {1: 1, 4: 7}


# -------------------------------------------------------------- parse input

def test_source_for(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1", encoding="utf-8")
    assert ma.source_for(p) == "x = 1"


def test_source_for_unreadable(tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(b"\xff\xfe\x00")
    assert ma.source_for(p) is None
    assert ma.source_for(tmp_path / "missing.py") is None


def test_parse_tree_valid_and_invalid():
    assert ma.parse_tree("def f():\n    pass\n") is not None
    assert ma.parse_tree("def f(:\n") is None


def test_parsed_pair_and_parse_source(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n")
    pair = ma.parsed_pair("x = 1\n")
    assert pair == ("x = 1\n", pair[1])
    source = "def f(:\n"
    assert ma.parsed_pair(source) is None
    assert ma.parse_source(tmp_path / "missing.py") is None
    got = ma.parse_source(p)
    assert got is not None and got[0] == "x = 1\n"