#!/usr/bin/env python3
"""CRAP audit for the .NET solution — the verifier half of the two-agent loop.

Runs the test suite with coverage, runs crap4dotnet analysis, gates on
CRAP < threshold for every non-test method, and writes:

    crap-report.json   full tool report (methods[], stats, warnings)
    crap-queue.md      markdown work queue, worst first, with file:line

Exit code: 0 when the gate passes, 1 when methods need work.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from functools import partial
from operator import itemgetter
from pathlib import Path

try:  # pytest: imported as scripts.dotnet.audit
    from scripts.dotnet.coverage_merge import write_merged
except ImportError:  # standalone: python3 scripts/dotnet/audit.py
    from coverage_merge import write_merged


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
REPORT = REPO / "crap-report.json"
QUEUE = REPO / "crap-queue.md"
RESULTS_DIR = REPO / "artifacts" / "test-results"


CSHARP_PROJECT_TYPE = "{9A19103F-16F7-4668-BE54-9A1E7A4F7556}"
SLNX_TMP_SUFFIX = ".quality-loop-tmp.sln"


def solution_path(repo: Path | None = None) -> Path:
    """The repo's solution — .slnx first (.NET 9+ XML format), then .sln.

    Root files first, then the shallowest nested one. `dotnet build` and
    `dotnet test` consume .slnx natively; the CRAP audit shims a .slnx to a
    transient classic .sln for crap4dotnet (see slnx_to_sln).
    """
    repo = repo or REPO
    for cand in (*repo.glob("*.slnx"), *repo.glob("*.sln"),
                 *sorted((*repo.rglob("*.slnx"), *repo.rglob("*.sln")),
                         key=lambda p: (len(p.parts), str(p)))):
        return cand
    raise SystemExit(f"ERROR: no *.slnx/*.sln found under {repo}")


def slnx_projects(slnx: Path) -> list[str]:
    """Project paths declared by a .slnx file, in document order.

    .slnx is plain XML: <Project Path="..."/> entries, optionally nested
    inside <Folder> elements (nesting is ignored — only membership matters).
    """
    try:
        root = ET.parse(str(slnx)).getroot()
    except ET.ParseError as e:
        raise SystemExit(f"ERROR: cannot parse {slnx}: {e}")
    paths = [p for p in ((el.get("Path") or "").strip() for el in root.iter("Project")) if p]
    if not paths:
        raise SystemExit(f"ERROR: {slnx} declares no <Project Path=.../> entries")
    return paths


def slnx_to_sln(slnx: Path) -> Path:
    """Materialize a transient classic .sln with the .slnx's membership.

    crap4dotnet rejects .slnx outright, so the CRAP audit analyzes through
    this shim instead: same projects, classic format, deterministic content
    (uuid5 GUIDs derived from the project path, so same .slnx ⇒ same bytes).
    The file lives next to the .slnx because the tool resolves entries
    relative to the .sln; run_tool() removes it afterwards.
    """
    lines = [
        "Microsoft Visual Studio Solution File, Format Version 12.00",
        "# Visual Studio Version 17",
        "VisualStudioVersion = 17.0.31903.59",
        "MinimumVisualStudioVersion = 10.0.40219.1",
    ]
    for rel in slnx_projects(slnx):
        entry = rel.replace("/", "\\")
        guid = uuid.uuid5(uuid.NAMESPACE_URL, rel.replace("\\", "/").lower())
        name = Path(rel).stem
        lines += [
            f'Project("{CSHARP_PROJECT_TYPE}") = "{name}", "{entry}", "{{{str(guid).upper()}}}"',
            "EndProject",
        ]
    lines += [
        "Global",
        "\tGlobalSection(SolutionConfigurationPlatforms) = preSolution",
        "\t\tDebug|Any CPU = Debug|Any CPU",
        "\t\tRelease|Any CPU = Release|Any CPU",
        "\tEndGlobalSection",
        "EndGlobal",
        "",
    ]
    sln = slnx.with_name(slnx.stem + SLNX_TMP_SUFFIX)
    sln.write_text("\n".join(lines), encoding="utf-8")
    return sln


def test_projects(repo: Path | None = None) -> list[Path]:
    """Every *.Tests.csproj — root first, then shallowest nested, sorted."""
    repo = repo or REPO
    projects = list(dict.fromkeys((*repo.glob("*.Tests.csproj"),
                 *sorted(repo.rglob("*.Tests.csproj"), key=lambda p: (len(p.parts), str(p))))))
    if not projects:
        raise SystemExit(f"ERROR: no *.Tests.csproj found under {repo}")
    return projects


def test_namespaces(repo: Path | None = None) -> list[str]:
    """Namespace prefixes of the test projects; the gate excludes them by default."""
    return [p.stem for p in test_projects(repo)]

CAPS: dict[int, int] = {3: 58, 4: 70}  # column index -> max chars; missing = no cap


def home_tool_path() -> Path:
    home_tool = Path.home() / ".dotnet" / "tools" / "dotnet-crap"
    if home_tool.is_file():
        return home_tool
    raise SystemExit("ERROR: dotnet-crap not found. Install with: dotnet tool install -g crap4dotnet")


def dotnet_crap_path() -> Path:
    exe = shutil.which("dotnet-crap")
    if exe:
        return Path(exe)
    return home_tool_path()


def print_tail(proc: subprocess.CompletedProcess) -> None:
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-5:])
    if tail:
        print(tail)


MERGED_NAME = "merged.cobertura.xml"


def run_tests_with_coverage() -> Path | None:
    """Run the solution's whole test suite and return the merged coverage file.

    `dotnet test` on the solution runs every test project (one
    coverage.cobertura.xml each); the per-project files are merged so the
    gate sees the whole suite.
    """
    test_projects()  # fail fast with the discovery error before testing
    print("==> Running tests with coverage (dotnet test <solution>)...")
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    proc = subprocess.run(
        [
            "dotnet", "test", str(solution_path()),
            "--collect:XPlat Code Coverage",
            "--results-directory", str(RESULTS_DIR),
            "-v", "quiet",
        ],
        capture_output=True, text=True,
    )
    print_tail(proc)
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: dotnet test failed (exit {proc.returncode})")
    return merge_results(RESULTS_DIR)


def newest_coverages(results_dir: Path | None = None) -> list[Path]:
    """Every coverage.cobertura.xml under the results dir, oldest first."""
    results_dir = results_dir or RESULTS_DIR
    return sorted(results_dir.rglob("coverage.cobertura.xml"),
                  key=lambda p: (p.stat().st_mtime, str(p)))


def merge_results(results_dir: Path | None = None) -> Path | None:
    """Merge every coverage file found; one file is returned as-is."""
    covs = newest_coverages(results_dir)
    if not covs:
        return None
    if len(covs) == 1:
        return covs[0]
    out = (results_dir or RESULTS_DIR) / MERGED_NAME
    print(f"==> Merging {len(covs)} coverage files -> {out.name}...")
    return write_merged(covs, out)


def warn_stale(covs: list[Path]) -> None:
    if covs:
        print(f"==> WARNING: --skip-tests reusing {len(covs)} file(s), "
              f"newest {covs[-1]} (stale coverage)")


def choose_coverage(args: argparse.Namespace) -> Path | None:
    if args.skip_tests:
        covs = newest_coverages()
        warn_stale(covs)
        return merge_results(RESULTS_DIR)
    return run_tests_with_coverage()


def ensure_coverage(cov: Path | None) -> Path:
    if not cov:
        raise SystemExit(f"ERROR: no coverage.cobertura.xml found in {RESULTS_DIR}")
    return cov


def run_tool(cov: Path, threshold: int) -> None:
    print(f"==> Coverage: {cov}")
    print(f"==> Running dotnet-crap analyze (threshold={threshold})...")
    env = {**os.environ, "DOTNET_ROLL_FORWARD": "LatestMajor"}
    tool = dotnet_crap_path()
    solution = solution_path()
    shim: Path | None = None
    try:
        if solution.suffix.lower() == ".slnx":
            # crap4dotnet rejects .slnx; analyze the same membership via a
            # transient classic .sln instead (removed in finally).
            shim = slnx_to_sln(solution)
            solution = shim
        subprocess.run(  # tool exits 1 when crappy methods exist; not a script failure
            [str(tool), "analyze", str(solution),
             "--coverage", str(cov), "--threshold", str(threshold),
             "--output", str(REPORT)],
            env=env, check=False,
        )
    finally:
        if shim is not None:
            shim.unlink(missing_ok=True)


def include_or_not_tests(include_tests: bool, namespace: str) -> bool:
    return include_tests or not any(namespace.startswith(ns) for ns in test_namespaces())


def is_failing(method: dict, threshold: int, include_tests: bool) -> bool:
    if method["crap"] < threshold:
        return False
    return include_or_not_tests(include_tests, method["namespace"])


def failing_methods(report: dict, threshold: int, include_tests: bool) -> list[dict]:
    methods = report["methods"]
    return sorted(filter(partial(is_failing, threshold=threshold, include_tests=include_tests), methods),
                  key=itemgetter("crap"), reverse=True)


def col_width(clipped: list[list[str]], col: int) -> int:
    return max(len(row[col]) for row in clipped)


def column_widths(clipped: list[list[str]]) -> list[int]:
    return [col_width(clipped, col) for col in range(len(clipped[0]))]


def cap_for(cap: int | None, text: str) -> int:
    return cap if cap is not None else len(text)


def clip_cell(text: str, cap: int | None) -> str:
    if len(text) <= cap_for(cap, text):
        return text
    return text[: cap - 1] + "…"


def clip_row(row: list[str]) -> list[str]:
    return [clip_cell(cell, CAPS.get(col)) for col, cell in enumerate(row)]


def format_row(row: list[str], widths: list[int]) -> str:
    return "| " + " | ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)) + " |"


def format_table(clipped: list[list[str]], widths: list[int]) -> str:
    return "\n".join(format_row(row, widths) for row in clipped)


def render_table(rows: list[list[str]]) -> str:
    """Aligned markdown table; caps wide columns (method, location) so the file stays readable."""
    clipped = [clip_row(row) for row in rows]
    return format_table(clipped, column_widths(clipped))


def row_for(method: dict) -> list[str]:
    rel = os.path.relpath(method["filePath"], REPO)
    return [
        f"{method['crap']:g}", f"{method['complexity']}", f"{method['coverage']:g}%",
        f"{method['className']}.{method['methodName']}",
        f"{rel}:{method['lineNumber']}",
    ]


def queue_rows(failing: list[dict]) -> list[list[str]]:
    rows = [["CRAP", "Cx", "Cov", "Method", "Location"],
            ["---", "---:", "---:", "---", "---"]]
    for method in failing:
        rows.append(row_for(method))
    return rows


def queue_lines(failing: list[dict], total: int, threshold: int) -> list[str]:
    return [
        render_table(queue_rows(failing)),
        "",
        f"**Gate**: {len(failing)} of {total} methods have CRAP >= {threshold} "
        f"(test project excluded). Full data in crap-report.json.",
        "",
        "Diagnosis (verifier): read the source at each location and add a fix recommendation.",
        "Fix (implementor): see /quality-fix template and AGENTS.md; re-run this audit after changes.",
    ]


def write_queue(report: dict, failing: list[dict], threshold: int) -> None:
    total = len(report["methods"])
    QUEUE.write_text("\n".join(queue_lines(failing, total, threshold)) + "\n")


def print_warnings(report: dict) -> None:
    for warning in report.get("warnings", []):
        print(f"[{warning['code']}] {warning['message']}")


def print_summary(report: dict, failing: list[dict], threshold: int) -> None:
    stats = report["stats"]
    total = len(report["methods"])
    print()
    print("\n".join(queue_lines(failing, total, threshold)))
    print()
    print(f"Analyzed {stats['methodCount']} methods | avg CRAP {stats['averageCrap']} | "
          f"median {stats['medianCrap']} | {stats['crappyMethodCount']} >= threshold")
    print(f"Gate: {len(failing)} of {total} methods >= {threshold} (test project excluded)")
    print_warnings(report)


def gate_exit(failing: list[dict], threshold: int) -> int:
    if failing:
        print(f"==> FAIL: {len(failing)} method(s) need work (see {QUEUE})")
        return 1
    print(f"==> PASS: all non-test methods have CRAP < {threshold}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=10, help="CRAP gate (default: 10)")
    parser.add_argument("--include-tests", action="store_true", help="do NOT exclude the test project from the gate")
    parser.add_argument("--skip-tests", action="store_true", help="reuse newest coverage instead of running dotnet test (stale; experiments only)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cov = ensure_coverage(choose_coverage(args))
    run_tool(cov, args.threshold)
    report = json.loads(REPORT.read_text())
    failing = failing_methods(report, args.threshold, args.include_tests)
    write_queue(report, failing, args.threshold)
    print_summary(report, failing, args.threshold)
    return gate_exit(failing, args.threshold)


if __name__ == "__main__":
    sys.exit(main())