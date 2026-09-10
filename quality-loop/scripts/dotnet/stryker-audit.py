#!/usr/bin/env python3
"""Stryker.NET mutation-testing audit for a .NET solution — the verifier half of
the two-agent loop.

Runs `dotnet-stryker` from every test project (*.Tests.csproj — Stryker
mutates per test project), then:
  - parses each project's newest StrykerOutput/<timestamp>/reports/mutation-report.json
  - writes stryker-queue.md (surviving mutants grouped by test project then
    file, worst-first — the implementor work queue, same shape as crap-queue.md)
  - exit 0 = pass; exit 1 = any project's mutation score below its thresholds.break

The test project's own `stryker-config.json` is used when present (repo policy:
project under test, mutation level, thresholds, reporters). Without one, this
audit writes a skill-bundled default config to a temp dir and pins the project
under test to the test project's ProjectReference — exactly one, or `--project`
must name it (Stryker.NET refuses to guess with multiple references and cannot
resolve bare/relative `project` names reliably; generated configs always use an
absolute csproj path). Determinism: mutation results are a pure function of
source, tests, tool version, and config (no randomness in mutant
generation/kill detection). Pin the tool version (see SKILL.md).

Usage: stryker-audit.py [--no-gate] [--project <csproj>] [-- stryker-args...]

  --no-gate          run and report, but always exit 0
  --project <csproj> project under test when the repo has no stryker-config.json
                     (absolute, or relative to the test project dir)
  -- <args>          extra args forwarded to dotnet-stryker, e.g.
                     -- --mutate 'Quezzi.Evals/Models/**'
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = "dotnet-stryker"
# Generic thresholds for repos without their own stryker-config.json; repos pin
# their own policy (project under test, break level) in the test project dir.
DEFAULT_THRESHOLDS = {"high": 80, "low": 60, "break": 25}
PROJECT_REF_RE = re.compile(r'<ProjectReference\s+Include="([^"]+)"')


def git_root(start: Path) -> Path | None:
    try:
        return Path(
            subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=start, text=True, stderr=subprocess.DEVNULL).strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def git_root_or(first: Path, second: Path) -> Path | None:
    return git_root(first) or git_root(second)


def find_repo() -> Path:
    # Spawned by the quality-loop skill with cwd = current project; also usable
    # standalone from inside the repo. Resolve the repo root from the process
    # cwd first, then from the script dir for legacy/embedded layouts, else
    # fall back to cwd.
    return git_root_or(Path.cwd(), Path(__file__).resolve().parent) or Path.cwd()


REPO = find_repo()
QUEUE = REPO / "stryker-queue.md"


def test_projects(repo: Path) -> list[Path]:
    """Every *.Tests.csproj — root first, then shallowest nested, sorted."""
    projects = list(dict.fromkeys((*repo.glob("*.Tests.csproj"),
                 *sorted(repo.rglob("*.Tests.csproj"), key=lambda p: (len(p.parts), str(p))))))
    if not projects:
        raise SystemExit(f"ERROR: no *.Tests.csproj found under {repo}")
    return projects


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path.home() / ".dotnet" / "tools" / name
    return str(candidate) if candidate.exists() else None


def referenced_projects(test_project: Path) -> list[Path]:
    """Non-test csproj paths referenced by a test project (backslash-tolerant,
    resolved relative to the test project's directory)."""
    td = test_project.parent
    text = test_project.read_text(encoding="utf-8")
    refs = []
    for m in PROJECT_REF_RE.findall(text):
        ref = Path(m.replace("\\", "/"))
        if not ref.is_absolute():
            ref = td / ref
        refs.append(ref.resolve())
    return refs


def choose_project_for(test_project: Path, explicit: str | None) -> Path:
    """The project under test for one test project: its single ProjectReference,
    or --project (absolute, or relative to the test project's directory)."""
    td = test_project.parent
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else (td / p).resolve()
    refs = referenced_projects(test_project)
    if len(refs) == 1:
        return refs[0]
    raise SystemExit(
        f"ERROR: cannot pick the project under test — {test_project.name} references "
        f"{len(refs)} projects. Add a `stryker-config.json` in {td} "
        "(`project` + thresholds) or pass --project <csproj>."
    )


def repo_config_for(test_project: Path) -> Path | None:
    """A test project's own stryker-config.json when it declares one."""
    config = test_project.parent / "stryker-config.json"
    return config if config.exists() else None


def default_config(project: Path, tmp: Path) -> Path:
    """Bundled default stryker config with an absolute project-under-test pin."""
    config = tmp / "stryker-config.json"
    config.write_text(json.dumps({
        "stryker-config": {
            "project": str(project),
            "mutation-level": "Standard",
            "coverage-analysis": "perTest",
            "additional-timeout": 2,
            "reporters": ["cleartext", "json", "html"],
            "thresholds": dict(DEFAULT_THRESHOLDS),
            "disable-bail": False,
            "verbosity": "info",
        }
    }, indent=2) + "\n", encoding="utf-8")
    return config


def thresholds_from(config: Path) -> dict:
    data = json.loads(config.read_text(encoding="utf-8"))
    return data.get("stryker-config", {}).get("thresholds", {})


def newest_json_report_for(test_project: Path) -> Path | None:
    reports = sorted(test_project.parent.glob("StrykerOutput/*/reports/mutation-report.json"))
    return reports[-1] if reports else None


def mutant_counts(data: dict) -> tuple[int, int, int, int]:
    """(killed, survived, timeout, no_coverage) over the mutants that count."""
    statuses = [m["status"] for f in data.get("files", {}).values() for m in f.get("mutants", [])]
    return (statuses.count("Killed"), statuses.count("Survived"),
            statuses.count("Timeout"), statuses.count("NoCoverage"))


def mutation_score(data: dict) -> float | None:
    """Stryker formula: detected (killed + timeout) over everything that counts
    (killed, survived, timeout, no coverage). CompileError/Ignored are skipped."""
    killed, survived, timeout, no_coverage = mutant_counts(data)
    denominator = killed + survived + timeout + no_coverage
    return 100.0 * (killed + timeout) / denominator if denominator else None


def project_sections(name: str, data: dict, score: float | None, thresholds: dict) -> list[str]:
    by_file: dict[str, list] = {}
    for path, info in data.get("files", {}).items():
        for m in info.get("mutants", []):
            if m.get("status") == "Survived":
                by_file.setdefault(path, []).append(m)

    lines = [
        f"## {name} — mutation score: **{score if score is not None else '(no mutants)'}** "
        f"(thresholds: high {thresholds.get('high', '-')} / low {thresholds.get('low', '-')} / "
        f"break {thresholds.get('break', '-')})",
        "",
    ]
    for path in sorted(by_file, key=lambda p: -len(by_file[p])):
        rel = path.replace(str(REPO) + "/", "")
        lines.append(f"### {rel} — {len(by_file[path])} survived")
        lines.append("")
        for m in sorted(by_file[path], key=lambda m: m["location"]["start"]["line"]):
            line = m["location"]["start"]["line"]
            lines.append(f"- `{rel}:{line}` {m['mutatorName']} -> {m.get('replacement', '')!r}")
        lines.append("")
    return lines


def queue_md(results: list[tuple[str, dict, float | None, dict]]) -> str:
    """Combined queue: one section per test project, worst-first files within."""
    lines = ["# Stryker queue (surviving mutants, worst-first)", ""]
    for name, data, score, thresholds in results:
        lines += project_sections(name, data, score, thresholds)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-gate", action="store_true", help="always exit 0")
    parser.add_argument("--project", default=None, help="project under test when the repo has no stryker-config.json")
    parser.add_argument("stryker_args", nargs="*", help="extra args after a literal `--`")
    args = parser.parse_args()
    extra = args.stryker_args[1:] if args.stryker_args and args.stryker_args[0] == "--" else args.stryker_args

    tool = find_tool(TOOL)
    if not tool:
        print(f"error: `{TOOL}` not found on PATH or ~/.dotnet/tools", file=sys.stderr)
        print("install: dotnet tool install --global dotnet-stryker", file=sys.stderr)
        return 2

    projects = test_projects(REPO)
    if args.project and len(projects) > 1:
        raise SystemExit(
            f"ERROR: --project is ambiguous with {len(projects)} test projects. "
            "Add a `stryker-config.json` in each test project dir (`project` + thresholds) instead."
        )

    env = {**os.environ, "DOTNET_ROLL_FORWARD": "LatestMajor"}  # testhost compat, same as the CRAP audit
    results: list[tuple[str, dict, float | None, dict]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for test_project in projects:
            tdir = test_project.parent
            own_config = repo_config_for(test_project)
            if own_config is not None:
                config_arg = str(own_config.resolve())
            else:
                config_arg = str(default_config(choose_project_for(test_project, args.project), Path(tmp)))
            thresholds = thresholds_from(Path(config_arg))

            print(f"==> Mutation testing {test_project.stem}...")
            proc = subprocess.run([tool, "--config-file", config_arg, *extra], cwd=tdir, env=env)
            if proc.returncode != 0:
                return proc.returncode

            report = newest_json_report_for(test_project)
            if report is None:
                print(f"error: no mutation-report.json found under {tdir}/StrykerOutput/",
                      file=sys.stderr)
                return 2
            print(f"report: {report}")
            data = json.loads(report.read_text(encoding="utf-8"))
            results.append((test_project.stem, data, mutation_score(data), thresholds))

    QUEUE.write_text(queue_md(results))

    failing = [name for name, _, score, thresholds in results
               if score is not None and score < float(thresholds.get("break", 0))]
    for name, data, score, thresholds in results:
        killed, survived, _, no_coverage = mutant_counts(data)
        break_at = float(thresholds.get("break", 0))
        score_txt = f"{score:.2f}%" if score is not None else "(no mutants)"
        print(f"stryker [{name}]: killed {killed}, survived {survived}, no coverage {no_coverage} "
              f"-> score {score_txt} (break-at {break_at:g}%)")
    print(f"queue: {QUEUE.name}")
    if failing and not args.no_gate:
        print(f"==> FAIL: {len(failing)} project(s) below break ({', '.join(failing)}) (see {QUEUE})")
        return 1
    if not args.no_gate:
        print("==> PASS: every test project meets its mutation break threshold")
    return 0


if __name__ == "__main__":
    sys.exit(main())