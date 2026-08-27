"""Tests for scripts/dotnet/warnings-audit.py — .NET build-warnings gate.

Covers MSBuild warning-line parsing (located, project-level, prefixed,
suffixed variants), dedup/sort of collected warnings, queue rendering, and
the exit-code mapping with the build faked out.
"""
from __future__ import annotations

import sys

import pytest

from tests.conftest import DOTNET_WARNINGS as wa


def run_main(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["warnings-audit.py", *args])
    return wa.main()


def setup_repo(monkeypatch, tmp_path):
    """Point the audit at tmp_path: repo root, report/queue targets, and a .sln."""
    monkeypatch.setattr(wa, "REPO", tmp_path)
    monkeypatch.setattr(wa, "REPORT", tmp_path / "warnings-report.json")
    monkeypatch.setattr(wa, "QUEUE", tmp_path / "warnings-queue.md")
    (tmp_path / "App.sln").write_text("")


# ---------------------------------------------------------------- parsing

def test_parse_warning_located_absolute():
    w = wa.parse_warning(
        "/repo/Quezzi.Web/Program.cs(12,5): warning CS0219: The variable 'x' is assigned but its value is never used [/repo/Quezzi.Web/Quezzi.Web.csproj]"
    )
    assert w == {
        "code": "CS0219",
        "filePath": "/repo/Quezzi.Web/Program.cs",
        "lineNumber": 12,
        "message": "The variable 'x' is assigned but its value is never used",
    }


def test_parse_warning_relative_path_resolved_against_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(wa, "REPO", tmp_path)
    w = wa.parse_warning("src/App.cs(3,1): warning IDE0005: Using directive is unnecessary [src/App.csproj]")
    assert w["filePath"] == str(tmp_path / "src" / "App.cs")
    assert w["lineNumber"] == 3


def test_parse_warning_no_location():
    w = wa.parse_warning("warning MSB3277: Found conflicts between different versions [App.csproj]")
    assert w["code"] == "MSB3277"
    assert w["lineNumber"] == 0
    assert w["message"] == "Found conflicts between different versions"


def test_parse_warning_msbuild_project_prefix():
    w = wa.parse_warning("2>src/App.cs(7,7): warning CS8321: The local function 'f' is declared but never used [src/App.csproj]")
    assert w["code"] == "CS8321"
    assert w["lineNumber"] == 7


def test_parse_warning_lowercase_code_and_brackets_in_message():
    w = wa.parse_warning("A.cs(1,1): warning cs0028: Use [MeansImplicitUse] or similar [A.csproj]")
    assert w["code"] == "CS0028"
    assert w["message"] == "Use [MeansImplicitUse] or similar"


def test_parse_warning_non_warning_line_is_none():
    assert wa.parse_warning("src/App.cs(3,1): error CS0219: boom") is None
    assert wa.parse_warning("Build succeeded.") is None
    assert wa.parse_warning("") is None


# ------------------------------------------------------------ collection

def test_collect_warnings_sorted_and_deduped():
    dup = "/r/A.cs(1,1): warning CS0219: assigned but unused [A.csproj]"
    other = "/r/B.cs(2,1): warning CS0168: declared but never used [B.csproj]"
    warnings = wa.collect_warnings(f"{dup}\n{other}\n{dup}")
    assert [w["code"] for w in warnings] == ["CS0168", "CS0219"]
    assert len(warnings) == 2


# ----------------------------------------------------------------- queue

def test_queue_rows_contains_gate_and_suppression_note(tmp_path, monkeypatch):
    monkeypatch.setattr(wa, "REPO", tmp_path)
    (tmp_path / "A.cs").write_text("")
    warnings = [{"code": "CS0219", "filePath": str(tmp_path / "A.cs"), "lineNumber": 1,
                 "message": "assigned but unused"}]
    md = "\n".join(wa.queue_lines(warnings))
    assert "CS0219" in md
    assert "A.cs:1" in md
    assert "**Gate**: 1 warning(s) found — 0 required" in md
    assert "<NoWarn>" in md


# -------------------------------------------------------------- exit codes

def test_main_clean_build_exits_0(monkeypatch, tmp_path, capsys):
    setup_repo(monkeypatch, tmp_path)
    proc = wa.subprocess.CompletedProcess([], 0, stdout="Build succeeded.\n", stderr="")

    def fake_build(solution):
        assert solution == tmp_path / "App.sln"
        return proc

    monkeypatch.setattr(wa, "run_build", fake_build)
    assert run_main(monkeypatch) == 0
    assert "PASS: clean build" in capsys.readouterr().out
    assert (tmp_path / "warnings-queue.md").exists()
    assert (tmp_path / "warnings-report.json").exists()


def test_main_warnings_remain_exits_1(monkeypatch, tmp_path):
    setup_repo(monkeypatch, tmp_path)
    out = "src/A.cs(1,1): warning CS0219: assigned but unused [src/A.csproj]\nBuild succeeded.\n"
    monkeypatch.setattr(wa, "run_build",
                        lambda solution: wa.subprocess.CompletedProcess([], 0, stdout=out, stderr=""))
    assert run_main(monkeypatch) == 1
    report = (tmp_path / "warnings-report.json").read_text()
    assert '"count": 1' in report


def test_main_build_failed_exits_2(monkeypatch, tmp_path):
    setup_repo(monkeypatch, tmp_path)
    out = "src/A.cs(1,1): warning CS0219: assigned but unused [src/A.csproj]\nBuild FAILED.\n"
    monkeypatch.setattr(wa, "run_build",
                        lambda solution: wa.subprocess.CompletedProcess([], 1, stdout=out, stderr=""))
    assert run_main(monkeypatch) == 2
    # Parsed warnings are still written even when the build fails.
    assert '"count": 1' in (tmp_path / "warnings-report.json").read_text()


def test_main_no_gate_exits_0_even_with_warnings(monkeypatch, tmp_path):
    setup_repo(monkeypatch, tmp_path)
    out = "src/A.cs(1,1): warning CS0219: assigned but unused [src/A.csproj]\n"
    monkeypatch.setattr(wa, "run_build",
                        lambda solution: wa.subprocess.CompletedProcess([], 0, stdout=out, stderr=""))
    assert run_main(monkeypatch, "--no-gate") == 0


def test_main_missing_solution_exits_error(monkeypatch, tmp_path):
    monkeypatch.setattr(wa, "REPO", tmp_path)
    with pytest.raises(SystemExit, match="no \\*.sln found"):
        run_main(monkeypatch)


def test_run_build_non_incremental_pins_language(monkeypatch):
    # --no-incremental is what makes the gate deterministic (up-to-date
    # projects can't hide warnings); UI language is pinned so localized
    # SDKs parse identically.
    calls = []
    monkeypatch.setattr(wa.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw)) or wa.subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    wa.run_build(wa.Path("/r/App.sln"))
    cmd, kw = calls[0]
    assert cmd[0] == "dotnet"
    assert "--no-incremental" in cmd
    assert kw["env"]["DOTNET_CLI_UI_LANGUAGE"] == "en"