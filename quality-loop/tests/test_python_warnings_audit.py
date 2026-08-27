"""Tests for scripts/python/warnings-audit.py — pyflakes lint gate.

Covers pyflakes discovery (the config-free walk with skipped dirs), the
reporter's collection of findings in the report schema, queue rendering, and
exit-code mapping, with pyflakes faked out so no system install is needed.
"""
from __future__ import annotations

import sys

import pytest

from tests.conftest import PY_WARNINGS as pw


def run_main(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["warnings-audit.py", *args])
    return pw.main()


def setup_repo(monkeypatch, tmp_path):
    """Point the audit at tmp_path: repo root and report/queue targets."""
    monkeypatch.setattr(pw, "REPO", tmp_path)
    monkeypatch.setattr(pw, "REPORT", tmp_path / "warnings-report.json")
    monkeypatch.setattr(pw, "QUEUE", tmp_path / "warnings-queue.md")


class FakeMessage:
    def __init__(self, filename, lineno, col, text):
        self.filename = filename
        self.lineno = lineno
        self.col = col
        self._text = text

    def __str__(self):
        return self._text


def fake_api(messages, unexpected=(), syntax=()):
    """A stand-in for pyflakes.api that feeds the reporter canned findings."""
    class Api:
        def __init__(self):
            self.calls = []

        def checkPath(self, filename, reporter):
            self.calls.append(filename)
            for text, lineno, col in messages:
                reporter.flake(FakeMessage(filename, lineno, col, text))
            for text in unexpected:
                reporter.unexpectedError(filename, text)
            for text, lineno in syntax:
                reporter.syntaxError(filename, text, lineno, 0, "line source")

    return Api()


def test_iter_py_files_skips_bad_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "REPO", tmp_path)
    (tmp_path / "src" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "app.py").write_text("")
    (tmp_path / "src" / "sub" / "mod.py").write_text("")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.py").write_text("")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "y.py").write_text("")
    (tmp_path / "README.md").write_text("")
    files = pw.iter_py_files()
    rels = sorted(p.relative_to(tmp_path).as_posix() for p in files)
    assert rels == ["src/app.py", "src/sub/mod.py"]


def test_reporter_collects_in_report_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "REPO", tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")
    api = fake_api([("'os' imported but unused", 3, 1)],
                   unexpected=["could not read file"],
                   syntax=[("invalid syntax", 9)])
    monkeypatch.setattr(pw, "pyflakes_api", api)
    warnings = pw.collect_warnings([tmp_path / "src" / "app.py"])
    assert len(warnings) == 3
    by_msg = {w["message"]: w for w in warnings}
    unused = by_msg["'os' imported but unused"]
    assert unused["filePath"].endswith("app.py")
    assert unused["lineNumber"] == 3 and unused["column"] == 1
    assert by_msg["could not read file"]["lineNumber"] == 0
    assert any("invalid syntax" in msg for msg in by_msg)


def test_main_findings_exit_1(monkeypatch, tmp_path):
    setup_repo(monkeypatch, tmp_path)
    (tmp_path / "app.py").write_text("import os\n")
    api = fake_api([("'os' imported but unused", 1, 1)])
    monkeypatch.setattr(pw, "pyflakes_api", api)
    assert run_main(monkeypatch) == 1
    assert '"count": 1' in (tmp_path / "warnings-report.json").read_text()
    assert "PASS" not in (tmp_path / "warnings-queue.md").read_text()


def test_main_clean_exit_0(monkeypatch, tmp_path, capsys):
    setup_repo(monkeypatch, tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(pw, "pyflakes_api", fake_api([]))
    assert run_main(monkeypatch) == 0
    assert "PASS: no pyflakes findings" in capsys.readouterr().out
    assert '"count": 0' in (tmp_path / "warnings-report.json").read_text()


def test_main_no_gate_exits_0_with_findings(monkeypatch, tmp_path):
    setup_repo(monkeypatch, tmp_path)
    (tmp_path / "app.py").write_text("import os\n")
    monkeypatch.setattr(pw, "pyflakes_api", fake_api([("'os' imported but unused", 1, 1)]))
    assert run_main(monkeypatch, "--no-gate") == 0


def test_main_missing_pyflakes_exits_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "REPO", tmp_path)
    monkeypatch.setattr(pw, "pyflakes_api", None)
    with pytest.raises(SystemExit, match="pyflakes not installed"):
        run_main(monkeypatch)


def test_queue_contains_gate_and_no_suppression(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "REPO", tmp_path)
    (tmp_path / "app.py").write_text("")
    warnings = [{"filePath": str(tmp_path / "app.py"), "lineNumber": 1, "column": 1,
                 "message": "'os' imported but unused"}]
    md = "\n".join(pw.queue_lines(warnings))
    assert "app.py:1" in md
    assert "**Gate**: 1 finding(s)" in md
    assert "No suppression exists" in md