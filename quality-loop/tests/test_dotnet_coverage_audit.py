"""Tests for scripts/dotnet/coverage-audit.py — multi-test-project handling.

Covers test-project discovery, solution discovery (.slnx first), exclusion
of EVERY test namespace from the authored gate, solution-wide test runs, and
merging of per-project coverage files. The module is dash-named, so it comes
via conftest's file loader.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.conftest import DOTNET_COVERAGE as ca
from tests.conftest import fake_proc


def run_main(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["coverage-audit.py", *args])
    return ca.main()


def point_repo(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ca, "REPO", tmp_path)
    monkeypatch.setattr(ca, "RESULTS_DIR", tmp_path / "artifacts" / "test-results")
    monkeypatch.setattr(ca, "REPORT", tmp_path / "coverage-report.json")
    monkeypatch.setattr(ca, "QUEUE", tmp_path / "coverage-queue.md")
    monkeypatch.setattr(ca, "HISTORY", tmp_path / "coverage-history.csv")
    monkeypatch.setattr(ca, "POLICY", tmp_path / "coverage-policy.json")


def method_xml(name: str, *numbers_hits: tuple[int, int]) -> str:
    lines = "".join(f'<line number="{n}" hits="{h}" branch="False" />' for n, h in numbers_hits)
    return f'<method name="{name}" signature="()"><lines>{lines}</lines></method>'


def doc(packages: str) -> ET.Element:
    return ET.fromstring(
        f'<coverage line-rate="0" branch-rate="0\">'
        f"<sources><source>/repo</source></sources><packages>{packages}</packages></coverage>")


def pkg(name: str, cls: str, filename: str, methods: str) -> str:
    return (f'<package name="{name}\"><classes>'
            f'<class name="{cls}" filename="{filename}\">'
            f"<methods>{methods}</methods><lines /></class>"
            f"</classes></package>")


# ------------------------------------------------------------ discovery

def test_test_projects_order(tmp_path, monkeypatch):
    point_repo(monkeypatch, tmp_path)
    (tmp_path / "Root.Tests.csproj").write_text("")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "A.Tests.csproj").write_text("")
    assert ca.test_projects() == [tmp_path / "Root.Tests.csproj", nested / "A.Tests.csproj"]
    assert ca.test_namespaces() == ["Root.Tests", "A.Tests"]


def test_solution_path_prefers_slnx(tmp_path):
    (tmp_path / "App.sln").write_text("")
    (tmp_path / "App.slnx").write_text("")
    assert ca.solution_path(tmp_path) == tmp_path / "App.slnx"


def test_solution_path_missing_raises(tmp_path):
    with pytest.raises(SystemExit, match=r"no \*\.slnx/\*\.sln found"):
        ca.solution_path(tmp_path)


# ------------------------------------------------------------ extraction

def test_extract_excludes_every_test_namespace(tmp_path, monkeypatch):
    point_repo(monkeypatch, tmp_path)
    (tmp_path / "A.Tests.csproj").write_text("")
    (tmp_path / "B.Tests.csproj").write_text("")
    root = doc(
        pkg("A.Tests", "T", "t.cs", method_xml("M", (1, 1)))
        + pkg("B.Tests.Sub", "U", "u.cs", method_xml("N", (2, 0)))
        + pkg("Lib", "C", "c.cs", method_xml("Work", (3, 0), (4, 0))))
    queue, authored, _, _ = ca.extract(root, 30)
    assert [m["methodName"] for m in authored] == ["Work"]
    assert [m["methodName"] for m in queue] == ["Work"]


# ------------------------------------------------------------ test runs

def test_run_tests_uses_solution_and_merges(tmp_path, monkeypatch):
    point_repo(monkeypatch, tmp_path)
    (tmp_path / "App.sln").write_text("")
    (tmp_path / "A.Tests.csproj").write_text("")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = tmp_path / "artifacts" / "test-results" / "g" / "coverage.cobertura.xml"
        out.parent.mkdir(parents=True)
        out.write_text("<coverage />")
        return fake_proc(0)

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    cov = ca.run_tests()
    assert calls[0][:2] == ["dotnet", "test"]
    assert str(tmp_path / "App.sln") in calls[0]
    assert cov.name == "coverage.cobertura.xml"


def test_run_tests_requires_a_test_project(tmp_path, monkeypatch):
    point_repo(monkeypatch, tmp_path)
    (tmp_path / "App.sln").write_text("")
    with pytest.raises(SystemExit, match="no \\*.Tests.csproj found"):
        ca.run_tests()


def test_newest_coverages_lists_all(tmp_path, monkeypatch):
    point_repo(monkeypatch, tmp_path)
    results = tmp_path / "artifacts" / "test-results"
    (results / "a").mkdir(parents=True)
    (results / "b").mkdir()
    c1 = results / "a" / "coverage.cobertura.xml"
    c2 = results / "b" / "coverage.cobertura.xml"
    c1.write_text("a")
    c2.write_text("b")
    assert ca.newest_coverages() == [c1, c2] or ca.newest_coverages() == [c2, c1]
    assert {c.name for c in ca.newest_coverages()} == {"coverage.cobertura.xml"}


# ------------------------------------------------------------ main

def test_main_explicit_coverage_passes(tmp_path, monkeypatch, capsys):
    point_repo(monkeypatch, tmp_path)
    cov = tmp_path / "cov.xml"
    cov.write_text(
        "<coverage><sources /><packages>"
        '<package name="Lib"><classes>'
        '<class name="C" filename="c.cs"><methods>'
        '<method name="Work" signature="()"><lines>'
        '<line number="1" hits="1" branch="False" />'
        '<line number="2" hits="1" branch="True" condition-coverage="100% (2/2)" />'
        '<line number="3" hits="1" branch="True" condition-coverage="100% (2/2)" />'
        "</lines></method></methods><lines>"
        '<line number="1" hits="1" branch="False" />'
        '<line number="2" hits="1" branch="True" condition-coverage="100% (2/2)" />'
        '<line number="3" hits="1" branch="True" condition-coverage="100% (2/2)" />'
        "</lines></class>"
        "</classes></package></packages></coverage>")
    assert run_main(monkeypatch, "--coverage", str(cov)) == 0
    assert "PASS" in capsys.readouterr().out
    assert (tmp_path / "coverage-report.json").exists()
