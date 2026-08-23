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
from functools import lru_cache, partial
from operator import itemgetter
from pathlib import Path


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


def solution_path(repo: Path | None = None) -> Path:
    """The repo's .sln — root files first, then the shallowest nested one."""
    repo = repo or REPO
    for cand in (*repo.glob("*.sln"),
                 *sorted(repo.rglob("*.sln"), key=lambda p: (len(p.parts), str(p)))):
        return cand
    raise SystemExit(f"ERROR: no *.sln found under {repo}")


@lru_cache(maxsize=None)
def _test_project(repo: Path) -> Path | None:
    for cand in (*repo.glob("*.Tests.csproj"),
                 *sorted(repo.rglob("*.Tests.csproj"), key=lambda p: (len(p.parts), str(p)))):
        return cand
    return None


def test_project(repo: Path | None = None) -> Path:
    """The repo's test project (*.Tests.csproj — root first, then shallowest)."""
    repo = repo or REPO
    project = _test_project(repo)
    if project is None:
        raise SystemExit(f"ERROR: no *.Tests.csproj found under {repo}")
    return project


def test_namespace(repo: Path | None = None) -> str:
    """Namespace prefix of the test project; the gate excludes it by default."""
    return test_project(repo).stem

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


def run_tests_with_coverage() -> Path | None:
    """Run the test suite and return the resulting coverage.cobertura.xml path."""
    print("==> Running tests with coverage (dotnet test + Testcontainers Postgres)...")
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    proc = subprocess.run(
        [
            "dotnet", "test", str(test_project()),
            "--collect:XPlat Code Coverage",
            "--results-directory", str(RESULTS_DIR),
            "-v", "quiet",
        ],
        capture_output=True, text=True,
    )
    print_tail(proc)
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: dotnet test failed (exit {proc.returncode})")
    return next(RESULTS_DIR.rglob("coverage.cobertura.xml"), None)


def newest_coverage() -> Path | None:
    matches = list(RESULTS_DIR.rglob("coverage.cobertura.xml"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def warn_stale(cov: Path | None) -> None:
    if cov:
        print(f"==> WARNING: --skip-tests reusing {cov} (stale coverage)")


def choose_coverage(args: argparse.Namespace) -> Path | None:
    if args.skip_tests:
        cov = newest_coverage()
        warn_stale(cov)
        return cov
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
    subprocess.run(  # tool exits 1 when crappy methods exist; not a script failure
        [str(tool), "analyze", str(solution_path()),
         "--coverage", str(cov), "--threshold", str(threshold),
         "--output", str(REPORT)],
        env=env, check=False,
    )


def include_or_not_tests(include_tests: bool, namespace: str) -> bool:
    return include_tests or not namespace.startswith(test_namespace())


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