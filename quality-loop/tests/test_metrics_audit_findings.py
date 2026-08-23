"""Tests for scripts/python/metrics-audit.py — findings rules and module MI.

Covers radon function-row mapping, the cyclomatic/arguments rule checks,
function/class flattening, and the module-level Maintainability Index
computation (including the MI < 20 failure path and severity counts).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import METRICS_AUDIT as ma


def mess_source(n: int = 300) -> str:
    """Arithmetic-only module with no comments: reliably MI < 20 for n ~ 300."""
    return "\n".join(f"x{i} = ((a{i}*b{i}) + c{i} - d{i}) * (e{i} + f{i})" for i in range(n))


# ---------------------------------------------------------- findings/rules

def test_function_row():
    fn = SimpleNamespace(fullname="mod.f", lineno=2, complexity=3)
    assert ma.function_row(fn, "mod.py", {2: 1}) == {
        "fullname": "mod.f", "file": "mod.py", "line": 2, "cyclomatic": 3, "args": 1,
    }


def test_rule_value():
    fn = SimpleNamespace(complexity=27, lineno=5)
    assert ma.rule_value(fn, {5: 9}, "cyclomatic") == 27
    assert ma.rule_value(fn, {5: 9}, "arguments") == 9
    assert ma.rule_value(fn, {}, "arguments") == 0


def test_finding_dict():
    rule = {"severity": "high", "message": lambda v: f"msg {v}", "remediation": "fix it"}
    f = ma.finding_dict("cyclomatic", rule, 26, "a.py", 3)
    assert f == {
        "ruleId": "cyclomatic", "severity": "high",
        "location": {"file": "a.py", "line": 3},
        "message": "msg 26", "remediation": "fix it",
    }


def test_append_rule_finding_triggers():
    ctx = {"findings": []}
    ma.append_rule_finding(ctx, "cyclomatic", 30, "a.py", 1)
    assert len(ctx["findings"]) == 1
    assert ctx["findings"][0]["ruleId"] == "cyclomatic"
    ma.append_rule_finding(ctx, "cyclomatic", 10, "a.py", 2)
    assert len(ctx["findings"]) == 1


def test_check_rule_and_scan_rules():
    ctx = {"findings": []}
    fn = SimpleNamespace(complexity=26, lineno=5)
    ma.scan_rules(ctx, fn, "a.py", {5: 8})
    assert [f["ruleId"] for f in ctx["findings"]] == ["cyclomatic", "arguments"]
    assert sorted(f["severity"] for f in ctx["findings"]) == ["high", "moderate"]


def test_collect_function_and_append_top():
    m1 = SimpleNamespace(name="m1", classname="C")
    out = []
    ma.collect_function(out, SimpleNamespace(name="C", methods=[m1]))
    assert out == [m1]
    f = SimpleNamespace(name="f", classname="")
    ma.append_top_function(out, f)
    ma.append_top_function(out, SimpleNamespace(name="g"))
    assert out == [m1, f]


def test_file_functions():
    ma.import_radon()
    src = "class C:\n    def m(self):\n        return 1\n\ndef top():\n    return 2\n"
    # radon cc_visit emits top-level fn, class, then the method again as a
    # standalone entry — the audit's real (deterministic) behavior on 6.0.1.
    assert [f.name for f in ma.file_functions(src)] == ["top", "m", "m"]


def test_scan_functions_rows_and_findings():
    ma.import_radon()
    ctx = {"findings": [], "functions": []}
    src = "def f(a, b, c, d, e, f, g, h):\n    return a\n\ndef g():\n    return 1\n"
    tree = __import__("ast").parse(src)
    ma.scan_functions(ctx, src, "mod.py", ma.function_args(tree))
    assert len(ctx["functions"]) == 2
    assert [f["ruleId"] for f in ctx["findings"]] == ["arguments"]


# ----------------------------------------------------------------- module MI

def test_module_mi_simple_high():
    assert ma.module_mi("def f():\n    return 1\n") > 50


def test_module_mi_mess_below_threshold():
    assert ma.module_mi(mess_source()) < 20


def test_append_mi_finding():
    ctx = {"findings": []}
    ma.append_mi_finding(ctx, "mess.py", 10.0)
    assert ctx["findings"][0]["ruleId"] == "mi"
    assert ctx["findings"][0]["location"] == {"file": "mess.py", "line": 1}
    ma.append_mi_finding(ctx, "ok.py", 50.0)
    assert len(ctx["findings"]) == 1


def test_scan_module_appends_entry_and_finding():
    ctx = {"findings": [], "modules": []}
    ma.scan_module(ctx, mess_source(), "mess.py")
    assert ctx["modules"] == [{"file": "mess.py", "mi": pytest.approx(11.7, abs=2.0), "sloc": 300}]
    assert ctx["findings"][0]["ruleId"] == "mi"


def test_scan_module_clean_module_no_finding():
    ctx = {"findings": [], "modules": []}
    ma.scan_module(ctx, "def f():\n    return 1\n", "ok.py")
    assert ctx["findings"] == []
    assert ctx["modules"][0]["mi"] > 50


def test_scan_file_and_new_context(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "REPO", tmp_path)
    ctx = ma.new_context()
    assert ctx == {"findings": [], "functions": [], "modules": [], "scanned": 0}
    p = tmp_path / "mod.py"
    p.write_text("def f():\n    return 1\n")
    ma.scan_file(ctx, p)
    assert ctx["scanned"] == 1
    assert len(ctx["functions"]) == 1
    assert ma.source_for(p) is not None


def test_scan_file_skips_broken_and_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "REPO", tmp_path)
    ctx = ma.new_context()
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n")
    ma.scan_file(ctx, bad)
    assert ctx["scanned"] == 0


def test_scan_all(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "REPO", tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("y = 2\n")
    ctx = ma.new_context()
    ma.scan_all(ctx)
    assert ctx["scanned"] == 2
    assert len(ctx["modules"]) == 2


def test_severity_counts():
    findings = [{"severity": "high"}, {"severity": "moderate"}, {"severity": "high"}]
    assert ma.severity_counts(findings) == {"high": 2, "moderate": 1}


def test_radon_version():
    parts = ma.radon_version().split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)