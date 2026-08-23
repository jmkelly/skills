"""Tests for scripts/python/audit.py — analysis and CRAP math.

Covers markdown table rendering helpers, AST/radon integration (function
enumeration, source reading, coverage attribution per function span) and
the CRAP record construction feeding the report.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.python import audit as pa


# ------------------------------------------------------------ table helpers

def test_col_width():
    assert pa.col_width([["a", "bb"], ["ccc", "d"]], 0) == 3
    assert pa.col_width([["a", "bb"], ["ccc", "d"]], 1) == 2


def test_column_widths():
    assert pa.column_widths([["a", "bb"], ["ccc", "d"]]) == [3, 2]


def test_cap_for():
    assert pa.cap_for(None, "abc") == 3
    assert pa.cap_for(2, "abc") == 2


@pytest.mark.parametrize(("value", "cap", "expected"), [
    ("abc", None, "abc"),
    ("abcd", 4, "abcd"),   # only strictly-longer cells are clipped
    ("abcde", 4, "abc…"),
    ("toolong", 3, "to…"),
])
def test_clip_cell(value, cap, expected):
    assert pa.clip_cell(value, cap) == expected


def test_clip_row_applies_caps():
    row = ["1", "2", "3", "M" * 100, "L" * 100]
    clipped = pa.clip_row(row)
    assert len(clipped[3]) == 58 and clipped[3].endswith("…")
    assert len(clipped[4]) == 70 and clipped[4].endswith("…")
    assert clipped[:3] == row[:3]


def test_format_row():
    assert pa.format_row(["x", "y"], [1, 1]) == "| x | y |"


def test_format_table():
    assert pa.format_table([["a", "b"], ["c", "d"]], [1, 1]) == "| a | b |\n| c | d |"


def test_render_table_aligns_columns():
    out = pa.render_table([["a", "bb", "ccc"], ["d", "e", "f"]])
    assert out.splitlines()[1] == "| d | e  | f   |"


# --------------------------------------------------------- ast / radon side

def test_append_function_node():
    out = []
    pa.append_function_node(out, ast.FunctionDef(name="f", args=None, body=[], decorator_list=[], lineno=1))
    pa.append_function_node(out, ast.Pass())
    assert len(out) == 1


def test_function_nodes_finds_nested_and_async():
    src = "def a():\n    pass\n\nasync def b():\n    pass\n\nclass C:\n    def m(self):\n        pass\n"
    lines = [n.lineno for n in pa.function_nodes(ast.parse(src))]
    assert lines == [1, 4, 8]


def test_function_endlines_map():
    src = "def f():\n    return 1\n\ndef g():\n    if x:\n        y()\n    return 2\n"
    assert pa.function_endlines(src) == {1: 2, 4: 7}


@pytest.mark.parametrize(("path", "include", "expected"), [
    (Path("tests/a.py"), False, True),
    (Path("test_a.py"), False, True),
    (Path("src/a.py"), False, False),
    (Path("tests/a.py"), True, False),
])
def test_test_skip(path, include, expected):
    assert pa.test_skip(path, include) is expected


def test_read_source(tmp_path):
    p = tmp_path / "ok.py"
    p.write_text("print(1)", encoding="utf-8")
    assert pa.read_source(p) == "print(1)"


def test_read_source_binary_gives_none(tmp_path):
    p = tmp_path / "bin.py"
    p.write_bytes(b"\xff\xfe\x00\x01")
    assert pa.read_source(p) is None


def test_read_source_missing_gives_none(tmp_path):
    assert pa.read_source(tmp_path / "missing.py") is None


def test_coverage_for_partial():
    fn = SimpleNamespace(lineno=1, endline=3)
    assert pa.coverage_for(fn, {1: 3}, {1, 2}, set()) == pytest.approx(2 / 3)


def test_coverage_for_falls_back_to_endline():
    fn = SimpleNamespace(lineno=1, endline=3)
    assert pa.coverage_for(fn, {}, {1, 2, 3}, set()) == 1.0


def test_coverage_for_excluded_lines_removed():
    fn = SimpleNamespace(lineno=1, endline=3)
    assert pa.coverage_for(fn, {1: 3}, {1}, {2, 3}) == 1.0


def test_coverage_for_fully_excluded_is_one():
    fn = SimpleNamespace(lineno=1, endline=3)
    assert pa.coverage_for(fn, {1: 3}, set(), {1, 2, 3}) == 1.0


@pytest.mark.parametrize(("crap", "threshold", "expected"), [(20, 10, "high"), (19.9, 10, "moderate"), (7, 3, "high")])
def test_severity_for(crap, threshold, expected):
    assert pa.severity_for(crap, threshold) == expected


def test_function_record_identity_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    fn = SimpleNamespace(name="bar", classname="Foo", lineno=7, complexity=3)
    rec = pa.function_record(fn, "pkg/mod.py", 0.5, 10)
    assert rec["methodName"] == "bar"
    assert rec["className"] == "Foo"
    assert rec["namespace"] == "pkg.mod"
    assert rec["lineNumber"] == 7


def test_function_record_path_and_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    fn = SimpleNamespace(name="bar", classname="Foo", lineno=7, complexity=3)
    rec = pa.function_record(fn, "pkg/mod.py", 0.5, 10)
    assert rec["filePath"] == str(tmp_path / "pkg" / "mod.py")
    assert rec["coverage"] == 50.0


def test_function_record_crap_and_severity(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    fn = SimpleNamespace(name="bar", classname="Foo", lineno=7, complexity=3)
    rec = pa.function_record(fn, "pkg/mod.py", 0.5, 10)
    assert rec["complexity"] == 3
    assert rec["crap"] == pytest.approx(3 ** 2 * 0.5 + 3)
    assert rec["severity"] == "moderate"


def test_function_record_top_level_classname_empty(monkeypatch, tmp_path):
    fn = SimpleNamespace(name="top", classname=None, lineno=1, complexity=1)
    rec = pa.function_record(fn, "m.py", 1.0, 10)
    assert rec["className"] == ""
    assert rec["crap"] == pytest.approx(1.0)


def test_collect_function_expands_methods_and_skips_classless():
    fn_class = SimpleNamespace(name="C", methods=["m1", "m2"])
    fn_plain = SimpleNamespace(name="f", classname="")
    out = []
    pa.collect_function(out, fn_class)
    pa.collect_function(out, fn_plain)
    assert out == ["m1", "m2", fn_plain]


def test_append_top_function_requires_classname():
    out = []
    pa.append_top_function(out, SimpleNamespace(name="a", classname=""))
    pa.append_top_function(out, SimpleNamespace(name="b"))
    assert [x.name for x in out] == ["a"]


def test_file_functions_expands_class_methods():
    pa.ensure_radon()
    src = "class C:\n    def m(self):\n        return 1\n\ndef top():\n    return 2\n"
    # radon cc_visit emits: top-level fn, class, then each method again as a
    # standalone entry -> the class method appears twice; that is the audit's
    # real behavior on radon 6, so pin it here for determinism.
    assert [f.name for f in pa.file_functions(src)] == ["top", "m", "m"]


def test_append_function_appends_record():
    fn = SimpleNamespace(name="m", classname="C", lineno=2, complexity=2, endline=3)
    methods = []
    pa.append_function(methods, fn, "a.py", {2, 3}, set(), {2: 3}, 10)
    assert len(methods) == 1
    assert methods[0]["coverage"] == 100.0


def test_append_file_methods_all_functions():
    pa.ensure_radon()
    src = "def a():\n    return 1\n\ndef b():\n    return 2\n"
    methods = []
    pa.append_file_methods(methods, src, "a.py", {1, 2, 4, 5}, set(), {1: 2, 4: 5}, 10)
    assert [m["methodName"] for m in methods] == ["a", "b"]
    assert all(m["coverage"] == 100.0 for m in methods)


def test_analyze_source_uses_executed_map():
    src = "def a():\n    return 1\n"
    methods = []
    pa.analyze_source(methods, src, "mod.py", {"mod.py": ({1, 2}, set())}, 10)
    assert len(methods) == 1
    assert methods[0]["coverage"] == 100.0


def test_analyze_source_missing_map_is_uncovered():
    src = "def a():\n    return 1\n"
    methods = []
    pa.analyze_source(methods, src, "mod.py", {}, 10)
    assert methods[0]["coverage"] == 0.0


def test_read_and_analyze_reads_file(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text("def a():\n    return 1\n")
    methods = []
    pa.read_and_analyze(methods, p, Path("mod.py"), {"mod.py": ({1, 2}, set())}, 10)
    assert len(methods) == 1


def test_read_and_analyze_skips_unreadable(tmp_path):
    p = tmp_path / "mod.py"
    p.write_bytes(b"\xff\xfe\x00")
    methods = []
    pa.read_and_analyze(methods, p, Path("mod.py"), {}, 10)
    assert methods == []


def test_analyze_file_skips_tests_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    t = tmp_path / "tests" / "t.py"
    t.parent.mkdir()
    t.write_text("def t():\n    pass\n")
    methods = []
    pa.analyze_file(methods, t, {}, False, 10)
    assert methods == []


def test_analyze_file_includes_tests_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    t = tmp_path / "tests" / "t.py"
    t.parent.mkdir()
    t.write_text("def t():\n    pass\n")
    methods = []
    pa.analyze_file(methods, t, {}, True, 10)
    assert [m["methodName"] for m in methods] == ["t"]


def test_analyze_walks_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("def t():\n    pass\n")
    methods = pa.analyze({}, False, 10)
    assert [m["methodName"] for m in methods] == ["a"]
    assert methods[0]["coverage"] == 0.0