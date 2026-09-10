"""Tests for scripts/dotnet/audit.py — the .NET CRAP verifier.

Covers dotnet-crap discovery, coverage selection, the tool invocation,
failing-method filtering, queue rendering and main(). Everything that would
shell out to dotnet is monkeypatched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.dotnet import audit as da
from tests.conftest import fake_proc


def sample_methods(tmp_path: Path) -> list[dict]:
    return [
        {"crap": 11, "complexity": 3, "coverage": 50, "methodName": "Bad",
         "className": "Core", "namespace": "Acme.Core",
         "filePath": str(tmp_path / "Core" / "Bad.cs"), "lineNumber": 5},
        {"crap": 12, "complexity": 4, "coverage": 25, "methodName": "Testy",
         "className": "Tests", "namespace": "Acme.Tests",
         "filePath": str(tmp_path / "Tests" / "T.cs"), "lineNumber": 9},
        {"crap": 3, "complexity": 1, "coverage": 100, "methodName": "Fine",
         "className": "Core", "namespace": "Acme.Core",
         "filePath": str(tmp_path / "Core" / "Fine.cs"), "lineNumber": 1},
    ]


def make_dotnet_repo(tmp_path: Path) -> Path:
    """A minimal repo with the files the .NET audit discovers (sln + test project)."""
    (tmp_path / "Acme.sln").write_text("")
    (tmp_path / "Acme.Tests.csproj").write_text("")
    return tmp_path


def make_dotnet_slnx_repo(tmp_path: Path) -> Path:
    """A minimal repo with a .slnx solution (Folder-nested, mixed separators)."""
    (tmp_path / "Acme.slnx").write_text(
        "<Solution>\n"
        '  <Folder Name="/src/">\n'
        '    <Project Path="src/Acme.Core/Acme.Core.csproj" />\n'
        "  </Folder>\n"
        '  <Project Path="tests\\Acme.Tests\\Acme.Tests.csproj" />\n'
        "</Solution>\n"
    )
    (tmp_path / "Acme.Tests.csproj").write_text("")
    return tmp_path


# ------------------------------------------------------- solution discovery

def test_solution_path_prefers_slnx_over_sln(tmp_path):
    make_dotnet_repo(tmp_path)
    (tmp_path / "Acme.slnx").write_text("<Solution />")
    assert da.solution_path(tmp_path) == tmp_path / "Acme.slnx"


def test_solution_path_finds_nested_slnx(tmp_path):
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "App.slnx").write_text("<Solution />")
    assert da.solution_path(tmp_path) == nested / "App.slnx"


def test_solution_path_missing_mentions_slnx(tmp_path):
    with pytest.raises(SystemExit, match=r"no \*\.slnx/\*\.sln found"):
        da.solution_path(tmp_path)


# ------------------------------------------------------------ slnx support

def test_slnx_projects_flat_and_nested(tmp_path):
    slnx = tmp_path / "Acme.slnx"
    slnx.write_text(
        '<Solution><Project Path="a/A.csproj" />'
        '<Folder Name="/t/"><Project Path="t\\B.Tests.csproj" /></Folder></Solution>'
    )
    assert da.slnx_projects(slnx) == ["a/A.csproj", "t\\B.Tests.csproj"]


def test_slnx_projects_rejects_empty_and_malformed(tmp_path):
    empty = tmp_path / "Empty.slnx"
    empty.write_text("<Solution />")
    with pytest.raises(SystemExit, match="declares no <Project"):
        da.slnx_projects(empty)
    bad = tmp_path / "Bad.slnx"
    bad.write_text("<Solution><Project")
    with pytest.raises(SystemExit, match="cannot parse"):
        da.slnx_projects(bad)


def test_slnx_to_sln_materializes_classic_format(tmp_path):
    make_dotnet_slnx_repo(tmp_path)
    sln = da.slnx_to_sln(tmp_path / "Acme.slnx")
    assert sln == tmp_path / "Acme.quality-loop-tmp.sln"
    text = sln.read_text()
    assert "Microsoft Visual Studio Solution File" in text
    # forward slashes normalized to classic backslash entries
    assert '"src\\Acme.Core\\Acme.Core.csproj"' in text
    assert '"tests\\Acme.Tests\\Acme.Tests.csproj"' in text
    # deterministic: same input -> byte-identical output
    digest = hash(text)
    sln.unlink()
    assert hash(da.slnx_to_sln(tmp_path / "Acme.slnx").read_text()) == digest
    (tmp_path / "Acme.quality-loop-tmp.sln").unlink()


def test_run_tool_shims_slnx_and_cleans_up(tmp_path, monkeypatch):
    make_dotnet_slnx_repo(tmp_path)
    monkeypatch.setattr(da, "REPO", tmp_path)
    monkeypatch.setattr(da, "REPORT", tmp_path / "crap-report.json")
    monkeypatch.setattr(da, "dotnet_crap_path", lambda: Path("/tools/dotnet-crap"))
    calls = []
    monkeypatch.setattr(da.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    da.run_tool(tmp_path / "coverage.xml", 10)
    assert calls[0][2] == str(tmp_path / "Acme.quality-loop-tmp.sln")
    assert not (tmp_path / "Acme.quality-loop-tmp.sln").exists()  # transient shim removed


def test_run_tool_passes_sln_directly_without_shim(tmp_path, monkeypatch):
    make_dotnet_repo(tmp_path)
    monkeypatch.setattr(da, "REPO", tmp_path)
    monkeypatch.setattr(da, "REPORT", tmp_path / "crap-report.json")
    monkeypatch.setattr(da, "dotnet_crap_path", lambda: Path("/tools/dotnet-crap"))
    calls = []
    monkeypatch.setattr(da.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    da.run_tool(tmp_path / "coverage.xml", 10)
    assert calls[0][2] == str(tmp_path / "Acme.sln")
    assert not list(tmp_path.glob("*.quality-loop-tmp.sln"))  # no shim for classic .sln


# ---------------------------------------------------------------- repo root

def test_git_root(monkeypatch):
    monkeypatch.setattr(da.subprocess, "check_output", lambda *a, **k: "/tmp/repo\n")
    assert da.git_root(Path.cwd()) == Path("/tmp/repo")


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, "git"), FileNotFoundError()])
def test_git_root_errors_return_none(monkeypatch, exc):
    monkeypatch.setattr(da.subprocess, "check_output", lambda *a, **k: (_ for _ in ()).throw(exc))
    assert da.git_root(Path.cwd()) is None


def test_git_root_or_and_find_repo(monkeypatch):
    monkeypatch.setattr(da, "git_root", lambda p: Path("/top"))
    assert da.git_root_or(Path("a"), Path("b")) == Path("/top")
    assert da.find_repo() == Path("/top")


# ------------------------------------------------------------ tool discovery

def test_home_tool_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = tmp_path / ".dotnet" / "tools" / "dotnet-crap"
    tool.parent.mkdir(parents=True)
    tool.write_text("")
    assert da.home_tool_path() == tool


def test_home_tool_path_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SystemExit, match="dotnet-crap not found"):
        da.home_tool_path()


def test_dotnet_crap_path_from_path(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: "/usr/bin/dotnet-crap")
    assert da.dotnet_crap_path() == Path("/usr/bin/dotnet-crap")


def test_dotnet_crap_path_falls_back_home(monkeypatch, tmp_path):
    monkeypatch.setattr(da.shutil, "which", lambda name: None)
    fallback = tmp_path / "dotnet-crap"
    fallback.write_text("")
    monkeypatch.setattr(da, "home_tool_path", lambda: fallback)
    assert da.dotnet_crap_path() == fallback


# ---------------------------------------------------------------- coverage

def test_newest_coverages_oldest_first(tmp_path, monkeypatch):
    results = tmp_path / "artifacts" / "test-results"
    monkeypatch.setattr(da, "RESULTS_DIR", results)
    (results / "run1").mkdir(parents=True)
    (results / "run2").mkdir()
    c1 = results / "run1" / "coverage.cobertura.xml"
    c2 = results / "run2" / "coverage.cobertura.xml"
    c1.write_text("a")
    c2.write_text("b")
    old = 1_000_000_000
    os.utime(c1, (old, old))
    os.utime(c2, (old + 100, old + 100))
    assert da.newest_coverages() == [c1, c2]


def test_newest_coverages_none(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "RESULTS_DIR", tmp_path)
    assert da.newest_coverages() == []


def test_merge_results_none_single_and_multi(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "RESULTS_DIR", tmp_path)
    assert da.merge_results(tmp_path) is None
    only = tmp_path / "coverage.cobertura.xml"
    only.write_text("<coverage />")
    assert da.merge_results(tmp_path) == only  # single file passes through
    (tmp_path / "other").mkdir()
    other = tmp_path / "other" / "coverage.cobertura.xml"
    other.write_text("<coverage />")
    merged = []

    def fake_write(covs, out):
        merged.append((list(covs), out))
        return out

    monkeypatch.setattr(da, "write_merged", fake_write)
    assert da.merge_results(tmp_path) == tmp_path / "merged.cobertura.xml"
    assert [c.name for c in merged[0][0]] == ["coverage.cobertura.xml"] * 2


def test_test_projects_order_and_namespaces(tmp_path):
    (tmp_path / "Root.Tests.csproj").write_text("")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "A.Tests.csproj").write_text("")
    (nested / "B.Tests.csproj").write_text("")
    assert da.test_projects(tmp_path) == [tmp_path / "Root.Tests.csproj",
                                           nested / "A.Tests.csproj",
                                           nested / "B.Tests.csproj"]
    assert da.test_namespaces(tmp_path) == ["Root.Tests", "A.Tests", "B.Tests"]


def test_test_projects_missing_raises(tmp_path):
    with pytest.raises(SystemExit, match="no \*.Tests.csproj found"):
        da.test_projects(tmp_path)


def test_warn_stale(capsys):
    da.warn_stale([Path("/x/a/coverage.cobertura.xml"), Path("/x/b/coverage.cobertura.xml")])
    out = capsys.readouterr().out
    assert "WARNING: --skip-tests reusing 2 file(s)" in out
    da.warn_stale([])
    assert capsys.readouterr().out == ""


def test_choose_coverage_skip_tests(monkeypatch, capsys):
    cov = Path("/x/coverage.xml")
    monkeypatch.setattr(da, "newest_coverages", lambda *a: [cov])
    monkeypatch.setattr(da, "merge_results", lambda *a: cov)
    assert da.choose_coverage(Namespace(skip_tests=True)) == cov
    assert "WARNING" in capsys.readouterr().out


def test_choose_coverage_runs_tests(monkeypatch):
    cov = Path("/x/coverage.xml")
    monkeypatch.setattr(da, "run_tests_with_coverage", lambda: cov)
    assert da.choose_coverage(Namespace(skip_tests=False)) == cov


def test_ensure_coverage_raises_when_missing():
    with pytest.raises(SystemExit, match="no coverage.cobertura.xml found"):
        da.ensure_coverage(None)


def test_ensure_coverage_returns_path(tmp_path):
    cov = tmp_path / "coverage.cobertura.xml"
    assert da.ensure_coverage(cov) == cov


def test_run_tests_with_coverage(tmp_path, monkeypatch):
    make_dotnet_repo(tmp_path)
    monkeypatch.setattr(da, "REPO", tmp_path)
    monkeypatch.setattr(da, "RESULTS_DIR", tmp_path / "test-results")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        (tmp_path / "test-results" / "g" / "coverage.cobertura.xml").parent.mkdir(parents=True)
        (tmp_path / "test-results" / "g" / "coverage.cobertura.xml").write_text("xml")
        return fake_proc(0)

    monkeypatch.setattr(da.subprocess, "run", fake_run)
    cov = da.run_tests_with_coverage()
    assert cov is not None and cov.name == "coverage.cobertura.xml"
    assert cov.exists()
    # the whole solution is tested, not one test project
    assert calls[0][:2] == ["dotnet", "test"]
    assert str(tmp_path / "Acme.sln") in calls[0]


def test_run_tests_with_coverage_failure(tmp_path, monkeypatch):
    make_dotnet_repo(tmp_path)
    monkeypatch.setattr(da, "REPO", tmp_path)
    monkeypatch.setattr(da, "RESULTS_DIR", tmp_path / "test-results")
    monkeypatch.setattr(da.subprocess, "run", lambda *a, **k: fake_proc(1))
    with pytest.raises(SystemExit, match="dotnet test failed"):
        da.run_tests_with_coverage()


def test_run_tests_with_coverage_requires_project(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "REPO", tmp_path)
    monkeypatch.setattr(da, "RESULTS_DIR", tmp_path / "test-results")
    monkeypatch.setattr(da.subprocess, "run", lambda *a, **k: fake_proc(0))
    with pytest.raises(SystemExit, match="no \\*.Tests.csproj found"):
        da.run_tests_with_coverage()


def test_run_tool(tmp_path, monkeypatch):
    make_dotnet_repo(tmp_path)
    calls = []
    tool = Path("/tools/dotnet-crap")
    monkeypatch.setattr(da, "dotnet_crap_path", lambda: tool)
    monkeypatch.setattr(da, "REPO", tmp_path)
    monkeypatch.setattr(da, "REPORT", tmp_path / "crap-report.json")

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))

    monkeypatch.setattr(da.subprocess, "run", fake_run)
    cov = tmp_path / "coverage.xml"
    da.run_tool(cov, 10)
    cmd, kw = calls[0]
    assert cmd == [str(tool), "analyze", str(tmp_path / "Acme.sln"),
                   "--coverage", str(cov), "--threshold", str(10),
                   "--output", str(tmp_path / "crap-report.json")]
    assert kw["check"] is False
    assert kw["env"]["DOTNET_ROLL_FORWARD"] == "LatestMajor"


def test_run_tool_requires_solution(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "REPO", tmp_path)
    monkeypatch.setattr(da, "dotnet_crap_path", lambda: Path("/tools/dotnet-crap"))
    monkeypatch.setattr(da.subprocess, "run", lambda *a, **k: fake_proc(0))
    with pytest.raises(SystemExit, match=r"no \*\.slnx/\*\.sln found"):
        da.run_tool(tmp_path / "coverage.xml", 10)


# ------------------------------------------------------------ failing filter

def test_include_or_not_tests(monkeypatch, tmp_path):
    make_dotnet_repo(tmp_path)
    monkeypatch.setattr(da, "REPO", tmp_path)
    assert not da.include_or_not_tests(False, "Acme.Tests.X")  # test namespace excluded
    assert da.include_or_not_tests(False, "Acme.Core")
    assert da.include_or_not_tests(True, "Acme.Tests.X")  # --include-tests overrides


def test_is_failing(monkeypatch, tmp_path):
    make_dotnet_repo(tmp_path)
    monkeypatch.setattr(da, "REPO", tmp_path)
    assert da.is_failing({"crap": 10, "namespace": "Acme.Core"}, 10, False)
    assert not da.is_failing({"crap": 9, "namespace": "Acme.Core"}, 10, False)
    assert not da.is_failing({"crap": 12, "namespace": "Acme.Tests"}, 10, False)
    assert da.is_failing({"crap": 12, "namespace": "Acme.Tests"}, 10, True)


def test_failing_methods(tmp_path, monkeypatch):
    make_dotnet_repo(tmp_path)
    monkeypatch.setattr(da, "REPO", tmp_path)
    methods = sample_methods(tmp_path)
    failing = da.failing_methods({"methods": methods}, 10, include_tests=False)
    assert [m["methodName"] for m in failing] == ["Bad"]  # Testy excluded, Fine passes
    failing_all = da.failing_methods({"methods": methods}, 10, include_tests=True)
    assert [m["methodName"] for m in failing_all] == ["Testy", "Bad"]


# ------------------------------------------------------------ table helpers

def test_col_width_and_column_widths():
    assert da.col_width([["a", "bb"], ["ccc", "d"]], 0) == 3
    assert da.column_widths([["a", "bb"], ["ccc", "d"]]) == [3, 2]


def test_cap_for_and_clip_cell():
    assert da.cap_for(None, "abc") == 3
    assert da.clip_cell("abc", 4) == "abc"
    assert da.clip_cell("abcde", 4) == "abc…"  # only strictly-longer cells clip
    assert da.clip_cell("ab", 4) == "ab"


def test_clip_row():
    clipped = da.clip_row(["1", "2", "3", "M" * 100, "L" * 100])
    assert len(clipped[3]) == 58 and len(clipped[4]) == 70


def test_format_row_and_table():
    assert da.format_row(["x", "y"], [1, 1]) == "| x | y |"
    assert da.format_table([["a", "b"], ["c", "d"]], [1, 1]) == "| a | b |\n| c | d |"


def test_render_table():
    out = da.render_table([["a", "bb", "ccc"], ["d", "e", "f"]])
    assert out.splitlines()[1] == "| d | e  | f   |"


# ------------------------------------------------------------ queue/report

def test_row_for(monkeypatch, tmp_path):
    monkeypatch.setattr(da, "REPO", tmp_path)
    method = sample_methods(tmp_path)[0]
    method["coverage"] = 50.0
    row = da.row_for(method)
    assert row == ["11", "3", "50%", "Core.Bad", "Core/Bad.cs:5"]


def test_queue_rows_and_lines():
    rows = da.queue_rows([])
    assert rows[:2] == [["CRAP", "Cx", "Cov", "Method", "Location"],
                        ["---", "---:", "---:", "---", "---"]]
    lines = da.queue_lines([], 3, 10)
    assert lines[0].startswith("| CRAP |")
    assert lines[2] == "**Gate**: 0 of 3 methods have CRAP >= 10 (test project excluded). Full data in crap-report.json."
    assert lines[-1].startswith("Fix (implementor):")


def test_write_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "QUEUE", tmp_path / "crap-queue.md")
    da.write_queue({"methods": sample_methods(tmp_path)}, [], 10)
    content = (tmp_path / "crap-queue.md").read_text()
    assert "**Gate**: 0 of 3 methods have CRAP >= 10" in content


def test_print_warnings(capsys):
    report = {"warnings": [{"code": "KE001", "message": "stale coverage"}]}
    da.print_warnings(report)
    assert "[KE001] stale coverage" in capsys.readouterr().out


def test_print_summary(capsys):
    report = {
        "methods": sample_methods(Path("/repo")),
        "stats": {"methodCount": 3, "averageCrap": 8.7, "medianCrap": 8.7, "crappyMethodCount": 1},
    }
    da.print_summary(report, [], 10)
    out = capsys.readouterr().out
    assert "Analyzed 3 methods | avg CRAP 8.7 | median 8.7 | 1 >= threshold" in out
    assert "Gate: 0 of 3 methods >= 10 (test project excluded)" in out


def test_gate_exit(capsys):
    assert da.gate_exit([], 10) == 0
    assert "PASS" in capsys.readouterr().out
    assert da.gate_exit([{"crap": 11}], 10) == 1
    assert "FAIL" in capsys.readouterr().out


# -------------------------------------------------------------------- main

def test_parse_args(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["audit.py"])
    args = da.parse_args()
    assert args.threshold == 10
    monkeypatch.setattr(sys, "argv", ["audit.py", "--threshold", "12", "--include-tests", "--skip-tests"])
    args = da.parse_args()
    assert args.threshold == 12
    assert args.include_tests and args.skip_tests


def test_main_clean(tmp_path, monkeypatch):
    make_dotnet_repo(tmp_path)
    cov = tmp_path / "coverage.xml"
    monkeypatch.setattr(da, "choose_coverage", lambda args: cov)
    calls = []
    monkeypatch.setattr(da, "run_tool", lambda c, t: calls.append((c, t)))
    monkeypatch.setattr(da, "REPORT", tmp_path / "crap-report.json")
    monkeypatch.setattr(da, "QUEUE", tmp_path / "crap-queue.md")
    monkeypatch.setattr(da, "REPO", tmp_path)
    methods = sample_methods(tmp_path)
    methods[0]["crap"] = 9  # everything below the gate
    report = {"methods": methods,
              "stats": {"methodCount": 3, "averageCrap": 8.0, "medianCrap": 9.0, "crappyMethodCount": 0},
              "warnings": []}
    (tmp_path / "crap-report.json").write_text(json.dumps(report))
    monkeypatch.setattr(sys, "argv", ["audit.py"])
    assert da.main() == 0
    assert calls == [(cov, 10)]
    assert (tmp_path / "crap-queue.md").exists()


def test_main_failing(tmp_path, monkeypatch):
    make_dotnet_repo(tmp_path)
    cov = tmp_path / "coverage.xml"
    monkeypatch.setattr(da, "choose_coverage", lambda args: cov)
    monkeypatch.setattr(da, "run_tool", lambda c, t: None)
    monkeypatch.setattr(da, "REPORT", tmp_path / "crap-report.json")
    monkeypatch.setattr(da, "QUEUE", tmp_path / "crap-queue.md")
    monkeypatch.setattr(da, "REPO", tmp_path)
    methods = sample_methods(tmp_path)
    methods[0]["crap"] = 99
    report = {"methods": methods,
              "stats": {"methodCount": 3, "averageCrap": 38.0, "medianCrap": 12.0, "crappyMethodCount": 1},
              "warnings": [{"code": "X", "message": "warn"}]}
    (tmp_path / "crap-report.json").write_text(json.dumps(report))
    monkeypatch.setattr(sys, "argv", ["audit.py"])
    assert da.main() == 1
    queue = (tmp_path / "crap-queue.md").read_text()
    assert "| 99" in queue
    assert "**Gate**: 1 of 3 methods have CRAP >= 10" in queue