#!/usr/bin/env python3
"""CRAP audit for Python projects — the verifier half of the two-agent loop.

Python equivalent of scripts/dotnet/audit.py: runs the test suite under
coverage.py, computes cyclomatic complexity per function with radon, gates on
CRAP < threshold for every non-test function, and writes:

    crap-report.json   same schema as the .NET tool (methods[], stats, warnings)
    crap-queue.md      markdown work queue, worst first, with file:line

Exit code: 0 when the gate passes, 1 when functions need work.

CRAP = complexity^2 * (1 - coverage) + complexity (Change Risk Anti-Patterns).
Coverage is per-function: the fraction of the function's line span that
coverage.py recorded as executed (excluded `# pragma: no cover` lines don't
count against it).

Requirements: `pip install radon coverage` (and the project's own test deps,
e.g. pytest). Test files (tests/, test_*.py, *_test.py) are excluded from the
gate like the .NET test project unless --include-tests.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import statistics
import subprocess
import sys
from functools import partial
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
    # standalone from inside the repo. Repo root = git toplevel from the process
    # cwd, then from the script dir (legacy layouts), else fall back to cwd.
    return git_root_or(Path.cwd(), Path(__file__).resolve().parent) or Path.cwd()


REPO = find_repo()
REPORT = REPO / "crap-report.json"
QUEUE = REPO / "crap-queue.md"
COV_JSON = REPO / "artifacts" / "coverage.json"

SKIPPED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
                ".pytest_cache", "artifacts", "build", "dist", "site-packages"}

CAPS: dict[int, int] = {3: 58, 4: 70}

FUNCTION_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef)


def is_test_dir(rel: Path) -> bool:
    return any(p in ("tests", "test") for p in rel.parts)


def is_test_leaf(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test.py")


def is_test_file(rel: Path) -> bool:
    return is_test_dir(rel) or is_test_leaf(rel.name)


def kept_dirs(dirs: list[str]) -> list[str]:
    return list(filter(lambda d: d not in SKIPPED_DIRS, dirs))


def prune(dirs: list[str]) -> None:
    dirs[:] = kept_dirs(dirs)


def add_py_file(files: list[Path], root: str, name: str) -> None:
    if name.endswith(".py"):
        files.append(Path(root) / name)


def add_py_files(files: list[Path], root: str, names: list[str]) -> None:
    for name in names:
        add_py_file(files, root, name)


def iter_py_files() -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(REPO):
        prune(dirs)
        add_py_files(files, root, names)
    return files


def print_tail(proc: subprocess.CompletedProcess) -> None:
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-5:])
    if tail:
        print(tail)


def ensure_pytest_ok(proc: subprocess.CompletedProcess) -> None:
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: pytest failed (exit {proc.returncode})")


def check_pytest_status(proc: subprocess.CompletedProcess) -> None:
    if proc.returncode == 5:
        print("WARNING: pytest collected no tests — every function counts as uncovered.")
        return
    ensure_pytest_ok(proc)


def handle_missing_coverage() -> None:
    if COV_JSON.exists():
        return
    # pytest found no tests and coverage collected no data: record honest
    # 0% coverage (every function counts as uncovered, matching the exit-5
    # warning above) instead of crashing the verifier.
    print("WARNING: coverage collected no data — writing empty coverage.json (0% coverage).")
    COV_JSON.parent.mkdir(parents=True, exist_ok=True)
    COV_JSON.write_text('{"files": {}}')


def dump_coverage_json() -> None:
    proc = subprocess.run(["coverage", "json", "-o", str(COV_JSON)], cwd=REPO, capture_output=True, text=True)
    if proc.returncode != 0:
        handle_missing_coverage()


def run_tests_with_coverage() -> None:
    """Run pytest under coverage.py and dump per-line data to COV_JSON."""
    print("==> Running tests with coverage (coverage run -m pytest)...")
    proc = subprocess.run(["coverage", "run", "-m", "pytest", "-q"], cwd=REPO, capture_output=True, text=True)
    print_tail(proc)
    check_pytest_status(proc)
    dump_coverage_json()


def executed_map(data: dict) -> dict[str, tuple[set[int], set[int]]]:
    """Map relative posix path -> (executed_lines, excluded_lines)."""
    return {
        path: (set(info.get("executed_lines", [])), set(info.get("excluded_lines", [])))
        for path, info in data.get("files", {}).items()
    }


def load_executed() -> dict[str, tuple[set[int], set[int]]]:
    if not COV_JSON.exists():
        raise SystemExit(f"ERROR: no {COV_JSON} — run the audit without --skip-tests first")
    return executed_map(json.loads(COV_JSON.read_text()))


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
    """Aligned markdown table; caps wide columns so the file stays readable."""
    clipped = [clip_row(row) for row in rows]
    return format_table(clipped, column_widths(clipped))


def append_function_node(out: list, node: ast.AST) -> None:
    if isinstance(node, FUNCTION_KINDS):
        out.append(node)


def function_nodes(tree: ast.Module) -> list:
    out: list = []
    for node in ast.walk(tree):
        append_function_node(out, node)
    return out


def function_endlines(source: str) -> dict[int, int]:
    """Map def-line -> body end line from the AST (radon's endline is
    unreliable for single-statement bodies, which would otherwise score
    fake-full coverage from a def-line-only span)."""
    return {node.lineno: node.end_lineno for node in function_nodes(ast.parse(source))}


def test_skip(rel: Path, include_tests: bool) -> bool:
    return include_tests is False and is_test_file(rel)


def read_source(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def coverage_for(fn, endlines: dict[int, int], executed, excluded) -> float:
    span = set(range(fn.lineno, endlines.get(fn.lineno, fn.endline) + 1)) - excluded
    if not span:
        return 1.0  # whole body excluded via `# pragma: no cover`
    return len(span & executed) / len(span)


def severity_for(crap: float, threshold: int) -> str:
    return "high" if crap >= 2 * threshold else "moderate"


def function_record(fn, rel: str, coverage: float, threshold: int) -> dict:
    crap = fn.complexity ** 2 * (1 - coverage) + fn.complexity
    return {
        "methodName": fn.name,
        "className": fn.classname or "",
        "namespace": Path(rel).with_suffix("").as_posix().replace("/", "."),
        "filePath": str(REPO / rel),
        "lineNumber": fn.lineno,
        "crap": round(crap, 2),
        "complexity": fn.complexity,
        "coverage": round(coverage * 100, 2),
        "severity": severity_for(crap, threshold),
    }


def class_methods(fn) -> list:
    return fn.methods


def extend_functions(out: list, methods: list) -> None:
    out += methods


def append_top_function(out: list, fn) -> None:
    if hasattr(fn, "classname"):
        out.append(fn)


def collect_function(out: list, fn) -> None:
    if hasattr(fn, "methods"):
        extend_functions(out, class_methods(fn))
        return
    append_top_function(out, fn)


def file_functions(source: str) -> list:
    """Function entries from cc_visit: expand class methods, skip class objects."""
    out: list = []
    for fn in radon_cc.cc_visit(source):
        collect_function(out, fn)
    return out


def append_function(methods: list[dict], fn, rel: str, executed, excluded,
                    endlines: dict[int, int], threshold: int) -> None:
    coverage = coverage_for(fn, endlines, executed, excluded)
    methods.append(function_record(fn, rel, coverage, threshold))


def append_file_methods(methods: list[dict], source: str, rel: str, executed, excluded,
                        endlines: dict[int, int], threshold: int) -> None:
    for fn in file_functions(source):
        append_function(methods, fn, rel, executed, excluded, endlines, threshold)


def analyze_source(methods: list[dict], source: str, rel: str,
                   executed_map: dict, threshold: int) -> None:
    executed, excluded = executed_map.get(rel, (set(), set()))
    append_file_methods(methods, source, rel, executed, excluded,
                        function_endlines(source), threshold)


def read_and_analyze(methods: list[dict], path: Path, rel_path: Path,
                     executed_map: dict, threshold: int) -> None:
    source = read_source(path)
    if source is not None:
        analyze_source(methods, source, rel_path.as_posix(), executed_map, threshold)


def analyze_file(methods: list[dict], path: Path, executed_map: dict,
                 include_tests: bool, threshold: int) -> None:
    rel_path = path.relative_to(REPO)
    if test_skip(rel_path, include_tests):
        return
    read_and_analyze(methods, path, rel_path, executed_map, threshold)


def analyze(executed_map: dict, include_tests: bool, threshold: int) -> list[dict]:
    methods: list[dict] = []
    for path in iter_py_files():
        analyze_file(methods, path, executed_map, include_tests, threshold)
    return methods


def mean_of(methods: list[dict]) -> float:
    return statistics.fmean(m["crap"] for m in methods)


def average_crap(methods: list[dict]) -> float:
    if methods:
        return mean_of(methods)
    return 0.0


def median_of(methods: list[dict]) -> float:
    return statistics.median(m["crap"] for m in methods)


def median_crap(methods: list[dict]) -> float:
    if methods:
        return median_of(methods)
    return 0.0


def is_crappy(method: dict, threshold: int) -> bool:
    return method["crap"] >= threshold


def crappy_count(methods: list[dict], threshold: int) -> int:
    return sum(is_crappy(m, threshold) for m in methods)


def build_stats(methods: list[dict], threshold: int) -> dict:
    return {
        "methodCount": len(methods),
        "averageCrap": round(average_crap(methods), 2),
        "medianCrap": round(median_crap(methods), 2),
        "crappyMethodCount": crappy_count(methods, threshold),
    }


def failing_methods(methods: list[dict], threshold: int) -> list[dict]:
    return sorted(filter(partial(is_crappy, threshold=threshold), methods),
                  key=itemgetter("crap"), reverse=True)


def row_for(method: dict) -> list[str]:
    rel = os.path.relpath(method["filePath"], REPO)
    display = f"{method['className']}.{method['methodName']}" if method["className"] else method["methodName"]
    return [
        f"{method['crap']:g}", f"{method['complexity']}", f"{method['coverage']:g}%",
        display, f"{rel}:{method['lineNumber']}",
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
        f"**Gate**: {len(failing)} of {total} functions have CRAP >= {threshold} "
        f"(test files excluded). Full data in crap-report.json.",
        "",
        "Diagnosis (verifier): read the source at each location and add a fix recommendation.",
        "Fix (implementor): reduce complexity first (extract, guard clauses); add real tests only when that is the cheap lever.",
    ]


def build_report(methods: list[dict], threshold: int) -> dict:
    return {"methods": methods, "stats": build_stats(methods, threshold), "warnings": []}


def write_artifacts(report: dict, failing: list[dict], threshold: int) -> None:
    REPORT.write_text(json.dumps(report, indent=2))
    lines = queue_lines(failing, report["stats"]["methodCount"], threshold)
    QUEUE.write_text("\n".join(lines) + "\n")


def print_summary(report: dict, failing: list[dict], threshold: int) -> None:
    stats = report["stats"]
    print()
    print("\n".join(queue_lines(failing, stats["methodCount"], threshold)))
    print()
    print(f"Analyzed {stats['methodCount']} functions | avg CRAP {stats['averageCrap']} | "
          f"median {stats['medianCrap']} | {stats['crappyMethodCount']} >= threshold")
    print(f"Gate: {len(failing)} of {stats['methodCount']} functions >= {threshold} (test files excluded)")


def gate_exit(failing: list[dict], threshold: int) -> int:
    if failing:
        print(f"==> FAIL: {len(failing)} function(s) need work (see {QUEUE})")
        return 1
    print(f"==> PASS: all functions have CRAP < {threshold}")
    return 0


def ensure_radon() -> None:
    global radon_cc
    try:
        import radon.complexity as radon_cc
    except ImportError:
        raise SystemExit("ERROR: radon not installed. Install with: pip install radon coverage")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("threshold", nargs="?", type=int, default=10, help="CRAP gate (default: 10)")
    parser.add_argument("--include-tests", action="store_true", help="do NOT exclude test files from the gate")
    parser.add_argument("--skip-tests", action="store_true", help="reuse last artifacts/coverage.json (stale; experiments only)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.skip_tests:
        run_tests_with_coverage()

    ensure_radon()

    methods = analyze(load_executed(), args.include_tests, args.threshold)
    report = build_report(methods, args.threshold)
    failing = failing_methods(methods, args.threshold)
    write_artifacts(report, failing, args.threshold)
    print_summary(report, failing, args.threshold)
    return gate_exit(failing, args.threshold)


if __name__ == "__main__":
    sys.exit(main())