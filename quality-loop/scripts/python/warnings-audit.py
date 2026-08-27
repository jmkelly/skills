#!/usr/bin/env python3
"""Lint-warnings audit for Python projects — the verifier half of the loop.

Runs pyflakes over every .py file in the repo (same walk and skipped dirs as
the CRAP audit) and gates on zero findings — unused imports/variables,
undefined names, shadowing, dead code: the compiler-warning analog for
Python. Writes:

    warnings-report.json  findings (filePath, lineNumber, column, message)
    warnings-queue.md     markdown work queue, ordered by file, with file:line

Exit code: 0 = no findings, 1 = findings remain (see queue), 2 = pyflakes
not installed.

pyflakes is deliberately config-free: there is no rule file to tune and no
suppression mechanism, so the only fix is removing the unused import or
defining the missing name — nothing to game.

Requirement: `pip install pyflakes`. Pin the version (deterministic rule set
and message text; the loop's determinism depends on it, see SKILL.md Notes).

Usage: warnings-audit.py [--no-gate]

  --no-gate   run the scan and write reports, but always exit 0.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
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
REPORT = REPO / "warnings-report.json"
QUEUE = REPO / "warnings-queue.md"

SKIPPED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
                ".pytest_cache", "artifacts", "build", "dist", "site-packages"}

CAPS: dict[int, int] = {0: 46, 1: 100}

try:
    pyflakes_api = importlib.import_module("pyflakes.api")
except ImportError:
    pyflakes_api = None


def kept_dirs(dirs: list[str]) -> list[str]:
    return list(filter(lambda d: d not in SKIPPED_DIRS, dirs))


def prune(dirs: list[str]) -> None:
    dirs[:] = kept_dirs(dirs)


def iter_py_files() -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(REPO):
        prune(dirs)
        files.extend(Path(root) / name for name in names if name.endswith(".py"))
    return files


def new_warning(file_path: str, line: int, column: int, message: str) -> dict:
    return {
        "filePath": str(file_path),
        "lineNumber": line,
        "column": column,
        "message": message,
    }


class PyflakesReporter:
    """Collects pyflakes findings in the warnings-report.json schema."""

    def __init__(self) -> None:
        self.warnings: list[dict] = []

    def unexpectedError(self, filename: str, msg: str) -> None:
        self.warnings.append(new_warning(filename, 0, 0, msg))

    def syntaxError(self, filename: str, msg: str, lineno: int | None,
                    offset: int | None, text: str | None) -> None:
        detail = f"{msg}: {text.strip()}" if text else msg
        self.warnings.append(new_warning(filename, lineno or 0, offset or 0, detail))

    def flake(self, message) -> None:
        self.warnings.append(new_warning(message.filename, message.lineno,
                                         message.col, str(message)))


def key_of(warning: dict) -> tuple:
    return (warning["filePath"], warning["lineNumber"], warning["column"])


def scan(reporter: PyflakesReporter, files: list[Path]) -> None:
    """Run pyflakes over every file, collecting findings into the reporter."""
    for path in files:
        pyflakes_api.checkPath(str(path), reporter)


def collect_warnings(files: list[Path]) -> list[dict]:
    reporter = PyflakesReporter()
    scan(reporter, files)
    return sorted(reporter.warnings, key=key_of)


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


def row_for(warning: dict) -> list[str]:
    rel = os.path.relpath(warning["filePath"], REPO)
    return [f"{rel}:{warning['lineNumber'] or '?'}", warning["message"]]


def queue_rows(warnings: list[dict]) -> list[list[str]]:
    rows = [["Location", "Message"],
            ["---", "---"]]
    for warning in warnings:
        rows.append(row_for(warning))
    return rows


def queue_lines(warnings: list[dict]) -> list[str]:
    return [
        render_table(queue_rows(warnings)),
        "",
        f"**Gate**: {len(warnings)} finding(s) (pyflakes) — 0 required. Full data in warnings-report.json.",
        "",
        "No suppression exists (pyflakes is config-free) — the fix is removing the unused "
        "import/variable or defining the missing name.",
        "",
        "Diagnosis (verifier): read the source at each location and add a fix recommendation.",
        "Fix (implementor): delete or use the unused name; add the missing import/definition; "
        "then re-run this audit.",
    ]


def write_artifacts(warnings: list[dict]) -> None:
    REPORT.write_text(json.dumps({"tool": "pyflakes", "warnings": warnings,
                                  "count": len(warnings)}, indent=2))
    QUEUE.write_text("\n".join(queue_lines(warnings)) + "\n")


def print_summary(warnings: list[dict]) -> None:
    files = {w["filePath"] for w in warnings}
    print()
    print(f"Findings: {len(warnings)} in {len(files)} file(s)")
    print(f"Reports: {REPORT.name}, {QUEUE.name}")


def gate_exit(warnings: list[dict]) -> int:
    if warnings:
        print(f"==> FAIL: {len(warnings)} finding(s) need work (see {QUEUE})")
        return 1
    print("==> PASS: no pyflakes findings")
    return 0


def ensure_pyflakes() -> None:
    if pyflakes_api is None:
        raise SystemExit("ERROR: pyflakes not installed. Install with: pip install pyflakes")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-gate", action="store_true", help="run and report, but always exit 0")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_pyflakes()
    files = iter_py_files()
    print(f"==> Scanning {len(files)} Python file(s) with pyflakes...")
    warnings = collect_warnings(files)
    write_artifacts(warnings)
    print_summary(warnings)
    return 0 if args.no_gate else gate_exit(warnings)


if __name__ == "__main__":
    sys.exit(main())