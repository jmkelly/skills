"""Tests for scripts/dotnet/stryker-audit.py — .NET mutation-testing gate.

Covers test-project discovery, ProjectReference parsing, project-under-test
selection, config selection (repo stryker-config.json vs the generated bundled
default), the mutation-score math, and the break gate, with dotnet-stryker
faked out.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import DOTNET_STRYKER as ds


def run_main(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["stryker-audit.py", *args])
    return ds.main()


def make_projects(tmp_path: Path, refs: tuple[str, ...] = ("LibA", "LibB")) -> Path:
    """A `Proj.Tests` project dir whose csproj references the given libs."""
    test_dir = tmp_path / "Proj.Tests"
    test_dir.mkdir()
    for lib in refs:
        (tmp_path / lib).mkdir()
        (tmp_path / lib / f"{lib}.csproj").write_text("<Project />")
    includes = "\n".join(f'    <ProjectReference Include="..\\{lib}\\{lib}.csproj" />' for lib in refs)
    (test_dir / "Proj.Tests.csproj").write_text(
        "<Project>\n  <ItemGroup>\n" + includes + "\n  </ItemGroup>\n</Project>\n"
    )
    return test_dir


def sample_mutation_report(survived: int = 0, killed: int = 3) -> dict:
    mutants = [
        {"status": "Killed", "mutatorName": "Arithmetic", "replacement": "+",
         "location": {"start": {"line": 1}}} for _ in range(killed)
    ]
    mutants += [
        {"status": "Survived", "mutatorName": "String", "replacement": "''",
         "location": {"start": {"line": 7}}} for _ in range(survived)
    ]
    return {"files": {"/repo/Proj.Lib/Program.cs": {"mutants": mutants}}}


def write_report(test_dir: Path, report: dict) -> None:
    out = test_dir / "StrykerOutput" / "2026-01-01.00-00-00" / "reports"
    out.mkdir(parents=True)
    (out / "mutation-report.json").write_text(json.dumps(report))


def make_named_projects(tmp_path: Path, name: str, refs: tuple[str, ...]) -> Path:
    """A `<name>` test-project dir whose csproj references the given libs."""
    test_dir = tmp_path / name
    test_dir.mkdir()
    for lib in refs:
        (tmp_path / lib).mkdir(exist_ok=True)
        (tmp_path / lib / f"{lib}.csproj").write_text("<Project />")
    includes = "\n".join(f'    <ProjectReference Include="..\\{lib}\\{lib}.csproj" />' for lib in refs)
    (test_dir / f"{name}.csproj").write_text(
        "<Project>\n  <ItemGroup>\n" + includes + "\n  </ItemGroup>\n</Project>\n"
    )
    return test_dir


@pytest.fixture
def patched(tmp_path, monkeypatch) -> Path:
    """REPO -> tmp_path with a test project referencing a single lib (LibA)."""
    test_dir = make_projects(tmp_path, refs=("LibA",))
    monkeypatch.setattr(ds, "REPO", tmp_path)
    monkeypatch.setattr(ds, "QUEUE", tmp_path / "stryker-queue.md")
    return test_dir


@pytest.fixture
def patched_multi(tmp_path, monkeypatch) -> Path:
    """Like patched, but the test project references LibA and LibB."""
    test_dir = make_projects(tmp_path, refs=("LibA", "LibB"))
    monkeypatch.setattr(ds, "REPO", tmp_path)
    monkeypatch.setattr(ds, "QUEUE", tmp_path / "stryker-queue.md")
    return test_dir


@pytest.fixture
def patched_two_projects(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """REPO -> tmp_path with TWO single-ref test projects (A.Tests, B.Tests)."""
    a = make_named_projects(tmp_path, "A.Tests", ("LibA",))
    b = make_named_projects(tmp_path, "B.Tests", ("LibB",))
    monkeypatch.setattr(ds, "REPO", tmp_path)
    monkeypatch.setattr(ds, "QUEUE", tmp_path / "stryker-queue.md")
    return a, b


def fake_subprocess(monkeypatch, calls: dict, returncode: int = 0) -> None:
    def run(cmd, **kw):
        calls["cmd"] = cmd
        calls["cwd"] = kw.get("cwd")
        calls["env"] = kw.get("env")
        if "--config-file" in cmd:
            i = cmd.index("--config-file")
            calls["config_file"] = cmd[i + 1]
            # the generated config lives in a temp dir that main() deletes, so
            # capture its contents at call time
            calls["config_text"] = Path(cmd[i + 1]).read_text()
        return subprocess.CompletedProcess([], returncode)

    monkeypatch.setattr(ds.subprocess, "run", run)


# ------------------------------------------------------- project discovery

def test_referenced_projects_parse_and_resolve(patched_multi):
    refs = ds.referenced_projects(patched_multi / "Proj.Tests.csproj")
    assert refs == [ds.REPO / "LibA" / "LibA.csproj", ds.REPO / "LibB" / "LibB.csproj"]


def test_test_projects_root_wins_over_nested(tmp_path):
    (tmp_path / "Proj.Tests.csproj").write_text("<Project />")
    nested = tmp_path / "Sub" / "Proj.Tests"
    nested.mkdir(parents=True)
    (nested / "Proj.Tests.csproj").write_text("<Project />")
    assert ds.test_projects(tmp_path) == [tmp_path / "Proj.Tests.csproj",
                                           nested / "Proj.Tests.csproj"]


def test_test_projects_missing_raises(tmp_path):
    with pytest.raises(SystemExit, match=r"no \*.Tests.csproj found"):
        ds.test_projects(tmp_path)


def test_choose_project_single_reference(tmp_path, monkeypatch):
    test_dir = make_projects(tmp_path, refs=("LibA",))
    monkeypatch.setattr(ds, "REPO", tmp_path)
    assert ds.choose_project_for(test_dir / "Proj.Tests.csproj", None) == (tmp_path / "LibA" / "LibA.csproj")
    assert test_dir == tmp_path / "Proj.Tests"


def test_choose_project_multiple_references_raises(patched_multi):
    with pytest.raises(SystemExit, match="stryker-config.json"):
        ds.choose_project_for(patched_multi / "Proj.Tests.csproj", None)


def test_choose_project_explicit_relative_resolves_against_test_dir(patched):
    proj = patched / "Proj.Tests.csproj"
    assert ds.choose_project_for(proj, "../LibA/LibA.csproj") == (ds.REPO / "LibA" / "LibA.csproj")
    # bare names stay relative to the test project dir (like ProjectReference)
    assert ds.choose_project_for(proj, "LibA/LibA.csproj") == (patched / "LibA" / "LibA.csproj")


def test_choose_project_explicit_absolute(patched):
    proj = patched / "Proj.Tests.csproj"
    assert ds.choose_project_for(proj, str(ds.REPO / "LibA" / "LibA.csproj")) == (ds.REPO / "LibA" / "LibA.csproj")


# ------------------------------------------------------- config selection

def test_repo_config_preferred_without_generated_default(patched, monkeypatch):
    repo_cfg = patched / "stryker-config.json"
    repo_cfg.write_text(json.dumps({"stryker-config": {"thresholds": {"break": 40}}}))
    calls: dict = {}
    fake_subprocess(monkeypatch, calls)
    write_report(patched, sample_mutation_report())
    assert run_main(monkeypatch) == 0
    assert calls["cmd"][0] == ds.find_tool("dotnet-stryker")
    assert calls["cmd"][1] == "--config-file"
    assert calls["cmd"][2] == str(repo_cfg.resolve())
    assert calls["cwd"] == patched


def test_generated_default_config_pins_absolute_project(patched, monkeypatch):
    calls: dict = {}
    fake_subprocess(monkeypatch, calls)
    write_report(patched, sample_mutation_report())
    assert run_main(monkeypatch) == 0
    cfg = json.loads(calls["config_text"])
    assert cfg["stryker-config"]["project"] == str(ds.REPO / "LibA" / "LibA.csproj")
    assert calls["cwd"] == patched
    assert calls["env"]["DOTNET_ROLL_FORWARD"] == "LatestMajor"


def test_generated_default_with_project_override(patched_multi, monkeypatch):
    calls: dict = {}
    fake_subprocess(monkeypatch, calls)
    write_report(patched_multi, sample_mutation_report())
    assert run_main(monkeypatch, "--project", "../LibB/LibB.csproj") == 0
    cfg = json.loads(calls["config_text"])
    assert cfg["stryker-config"]["project"] == str(ds.REPO / "LibB" / "LibB.csproj")


# ------------------------------------------------------- score + gate

def test_score_math_and_break_gate(patched, monkeypatch):
    queue = ds.QUEUE
    fake_subprocess(monkeypatch, {})
    write_report(patched, sample_mutation_report(survived=1, killed=4))
    assert run_main(monkeypatch) == 0  # 80% >= default break 25
    md = queue.read_text()
    assert "80.0" in md
    assert "Program.cs — 1 survived" in md


def test_gate_fails_below_break(patched, monkeypatch):
    fake_subprocess(monkeypatch, {})
    write_report(patched, sample_mutation_report(survived=4, killed=1))
    assert run_main(monkeypatch) == 1


def test_no_gate_always_passes(patched, monkeypatch):
    fake_subprocess(monkeypatch, {})
    write_report(patched, sample_mutation_report(survived=4, killed=1))
    assert run_main(monkeypatch, "--no-gate") == 0


def test_stryker_failure_passthrough(patched, monkeypatch):
    fake_subprocess(monkeypatch, {}, returncode=3)
    assert run_main(monkeypatch) == 3


def test_missing_tool_returns_2(patched, monkeypatch):
    monkeypatch.setattr(ds, "find_tool", lambda name: None)
    assert run_main(monkeypatch) == 2


# ------------------------------------------------------- multiple projects

def test_main_runs_every_test_project(patched_two_projects, monkeypatch):
    a, b = patched_two_projects
    calls: list = []

    def run(cmd, **kw):
        calls.append((cmd, kw.get("cwd")))
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(ds.subprocess, "run", run)
    write_report(a, sample_mutation_report(survived=0, killed=2))
    write_report(b, sample_mutation_report(survived=1, killed=3))
    assert run_main(monkeypatch) == 0  # 100% and 75% both >= default break 25
    assert [cwd for _, cwd in calls] == [a, b]
    md = (ds.REPO / "stryker-queue.md").read_text()
    assert "## A.Tests" in md and "## B.Tests" in md
    assert "100.0" in md and "75.0" in md


def test_main_fails_when_any_project_below_break(patched_two_projects, monkeypatch):
    a, b = patched_two_projects
    monkeypatch.setattr(ds.subprocess, "run",
                        lambda *args, **kw: subprocess.CompletedProcess([], 0))
    write_report(a, sample_mutation_report(survived=0, killed=2))
    write_report(b, sample_mutation_report(survived=4, killed=1))  # 20% < break 25
    assert run_main(monkeypatch) == 1
    md = (ds.REPO / "stryker-queue.md").read_text()
    assert "## B.Tests" in md


def test_main_project_flag_ambiguous_with_two_projects(patched_two_projects, monkeypatch):
    with pytest.raises(SystemExit, match="ambiguous"):
        run_main(monkeypatch, "--project", "LibA/LibA.csproj")