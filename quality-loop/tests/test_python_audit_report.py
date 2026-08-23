"""Tests for scripts/python/audit.py — stats, queue/report output, and main.

Covers CRAP summary statistics, gate evaluation (threshold boundary),
markdown queue rendering, report/artifact writing, and the CLI entry point
including --skip-tests / --include-tests handling.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts.python import audit as pa


# ---------------------------------------------------------- stats and gates

def test_average_crap_empty():
    assert pa.average_crap([]) == 0.0


def test_average_crap():
    assert pa.average_crap([{"crap": 2.0}, {"crap": 4.0}]) == 3.0


def test_median_crap_empty():
    assert pa.median_crap([]) == 0.0


def test_median_crap():
    assert pa.median_crap([{"crap": 1.0}, {"crap": 3.0}]) == 2.0


def test_is_crappy_boundary():
    assert pa.is_crappy({"crap": 10}, 10)
    assert not pa.is_crappy({"crap": 9.99}, 10)


def test_crappy_count():
    methods = [{"crap": 12}, {"crap": 3}, {"crap": 10}]
    assert pa.crappy_count(methods, 10) == 2


def test_build_stats():
    methods = [{"crap": 11.0}, {"crap": 7.0}]
    stats = pa.build_stats(methods, 10)
    assert stats == {"methodCount": 2, "averageCrap": 9.0, "medianCrap": 9.0, "crappyMethodCount": 1}


def test_failing_methods_sorted_worst_first():
    methods = [{"crap": 5}, {"crap": 12}, {"crap": 10}]
    assert [m["crap"] for m in pa.failing_methods(methods, 10)] == [12, 10]


# ------------------------------------------------------------ queue / report

def test_row_for(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    method = {
        "crap": 12, "complexity": 3, "coverage": 50,
        "methodName": "m", "className": "C",
        "filePath": str(tmp_path / "pkg" / "mod.py"), "lineNumber": 4,
    }
    assert pa.row_for(method) == ["12", "3", "50%", "C.m", "pkg/mod.py:4"]


def test_row_for_top_level_display():
    method = {
        "crap": 3, "complexity": 1, "coverage": 100,
        "methodName": "f", "className": "",
        "filePath": "/repo/a.py", "lineNumber": 1,
    }
    row = pa.row_for(method)
    assert row[3] == "f"


def test_queue_rows_headers():
    rows = pa.queue_rows([])
    assert rows[:2] == [["CRAP", "Cx", "Cov", "Method", "Location"],
                        ["---", "---:", "---:", "---", "---"]]


def test_queue_lines():
    lines = pa.queue_lines([], 5, 10)
    assert lines[0].startswith("| CRAP |")
    assert lines[2] == "**Gate**: 0 of 5 functions have CRAP >= 10 (test files excluded). Full data in crap-report.json."
    assert lines[-2].startswith("Diagnosis (verifier):")
    assert lines[-1].startswith("Fix (implementor):")


def test_build_report():
    methods = [{"crap": 1.0}]
    report = pa.build_report(methods, 10)
    assert report["methods"] == methods
    assert report["stats"]["methodCount"] == 1
    assert report["warnings"] == []


def test_write_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "REPO", tmp_path)
    monkeypatch.setattr(pa, "REPORT", tmp_path / "crap-report.json")
    monkeypatch.setattr(pa, "QUEUE", tmp_path / "crap-queue.md")
    report = {"methods": [], "stats": {"methodCount": 1, "averageCrap": 0, "medianCrap": 0, "crappyMethodCount": 0}, "warnings": []}
    pa.write_artifacts(report, [], 10)
    assert (tmp_path / "crap-report.json").exists()
    queue = (tmp_path / "crap-queue.md").read_text()
    assert "**Gate**: 0 of 1 functions have CRAP >= 10" in queue


def test_print_summary(capsys):
    report = {"stats": {"methodCount": 2, "averageCrap": 7.5, "medianCrap": 7.5, "crappyMethodCount": 1}}
    pa.print_summary(report, [], 10)
    out = capsys.readouterr().out
    assert "Analyzed 2 functions | avg CRAP 7.5 | median 7.5 | 1 >= threshold" in out
    assert "Gate: 0 of 2 functions >= 10 (test files excluded)" in out


def test_gate_exit_pass(capsys):
    assert pa.gate_exit([], 10) == 0
    assert "PASS" in capsys.readouterr().out


def test_gate_exit_fail(capsys):
    assert pa.gate_exit([{"crap": 12}], 10) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "crap-queue.md" in out


def test_ensure_radon_imports(monkeypatch):
    monkeypatch.setattr(pa, "radon_cc", None)
    pa.ensure_radon()
    assert pa.radon_cc is not None and pa.radon_cc.cc_visit


def test_ensure_radon_missing_raises(monkeypatch):
    monkeypatch.setattr(pa, "radon_cc", None)
    monkeypatch.setitem(sys.modules, "radon.complexity", None)
    with pytest.raises(SystemExit, match="radon not installed"):
        pa.ensure_radon()


# ------------------------------------------------------------------- main

def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["audit.py"])
    args = pa.parse_args()
    assert args.threshold == 10
    assert not args.include_tests
    assert not args.skip_tests


def test_parse_args_all_flags(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["audit.py", "12", "--include-tests", "--skip-tests"])
    args = pa.parse_args()
    assert args.threshold == 12
    assert args.include_tests
    assert args.skip_tests


def test_main_full_run(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(pa, "run_tests_with_coverage", lambda: calls.append("tests"))
    monkeypatch.setattr(pa, "load_executed", lambda: {})
    monkeypatch.setattr(pa, "analyze", lambda em, inc, thr: calls.append(("analyze", inc, thr)) or [])
    monkeypatch.setattr(pa, "write_artifacts", lambda *a: None)
    monkeypatch.setattr(pa, "print_summary", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["audit.py"])
    assert pa.main() == 0
    assert calls == ["tests", ("analyze", False, 10)]
    assert "PASS" in capsys.readouterr().out


def test_main_skip_tests_reuses_coverage(monkeypatch):
    calls = []
    monkeypatch.setattr(pa, "run_tests_with_coverage", lambda: calls.append("tests"))
    monkeypatch.setattr(pa, "load_executed", lambda: {})
    monkeypatch.setattr(pa, "analyze", lambda em, inc, thr: calls.append(("analyze", inc, thr)) or [])
    monkeypatch.setattr(pa, "write_artifacts", lambda *a: None)
    monkeypatch.setattr(pa, "print_summary", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["audit.py", "12", "--include-tests", "--skip-tests"])
    assert pa.main() == 0
    assert calls == [("analyze", True, 12)]