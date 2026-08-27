#!/usr/bin/env python3
"""Build-warnings audit for the .NET solution — the verifier half of the loop.

Runs `dotnet build --no-incremental` over the solution (non-incremental, so
up-to-date projects cannot hide their warnings), parses every
`: warning <code>:` line — compiler (CS), analyzer (CA/IDExxxx/custom),
NuGet (NU), MSBuild (MSB) — and writes:

    warnings-report.json  parsed warnings (code, filePath, lineNumber, message)
    warnings-queue.md     markdown work queue, ordered by code, with file:line

Exit code: 0 = clean build (zero warnings), 1 = warnings remain (see queue),
2 = build failed (a broken build is not a warnings outcome — the CRAP audit's
`dotnet test` reports it as a hard error too).

Suppression is repo policy, the idiomatic .NET mechanism: a *specific*
`<NoWarn>` entry in a csproj with a documented reason. Blanket
`<NoWarn>$(NoWarn);CS;CA</NoWarn>`-style suppression to dodge the gate is
against the anti-gaming rules (SKILL.md).

Determinism: `--no-incremental` forces a full compile of every project, and
`DOTNET_CLI_UI_LANGUAGE=en` pins MSBuild's UI language, so warnings are a pure
function of source, SDK/analyzer versions, and the csproj config. NuGet audit
warnings (NUxxxx, e.g. NU1901 vulnerabilities) are the one input that moves
over time — they follow feed advisory data; treat via NoWarn policy if they
are noise for a repo. Non-incremental builds cost more than incremental ones
(a few seconds to a couple of minutes on large solutions); the loop only pays
this before Stryker.

Usage: warnings-audit.py [--no-gate]

  --no-gate   run the build and write reports, but always exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

CAPS: dict[int, int] = {1: 46, 2: 100}  # column index -> max chars; missing = no cap

WARNING_RE = re.compile(r"^(?P<file>.*)\((?P<line>\d+)(?:,\d+)*\)$")
PROJECT_SUFFIX = re.compile(r" \[[^\]]+\]$")
PROJECT_PREFIX = re.compile(r"^\d+>")


def solution_path(repo: Path | None = None) -> Path:
    """The repo's .sln — root files first, then the shallowest nested one."""
    repo = repo or REPO
    for cand in (*repo.glob("*.sln"),
                 *sorted(repo.rglob("*.sln"), key=lambda p: (len(p.parts), str(p)))):
        return cand
    raise SystemExit(f"ERROR: no *.sln found under {repo}")


def print_tail(proc: subprocess.CompletedProcess) -> None:
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-5:])
    if tail:
        print(tail)


def run_build(solution: Path) -> subprocess.CompletedProcess:
    """Non-incremental build of the whole solution; returns the finished process."""
    print(f"==> Building {solution.name} non-incrementally (dotnet build --no-incremental)...")
    env = {**os.environ, "DOTNET_CLI_UI_LANGUAGE": "en"}
    return subprocess.run(
        ["dotnet", "build", str(solution), "--no-incremental", "--verbosity", "minimal", "--nologo"],
        capture_output=True, text=True, env=env,
    )


def location_of(prefix: str) -> tuple[str, int]:
    """Split the text before ': warning ' into (file path, line number)."""
    match = WARNING_RE.match(prefix)
    if match:
        return match.group("file"), int(match.group("line"))
    return prefix, 0


def normalized_path(raw: str) -> str:
    path = Path(re.sub(r'^"|"$', "", raw.strip()))
    if not path.is_absolute():
        path = REPO / path
    return str(path)


def parse_warning(line: str) -> dict | None:
    """Parse one MSBuild warning line; None when the line is not a warning.

    Handles located (`path(12,5): warning CS0219: msg [proj]`), project-level
    (`warning MSB3277: msg [proj]` — no colon/file prefix), prefixed and
    suffixed variants.
    """
    stripped = PROJECT_PREFIX.sub("", line.strip())
    mark = stripped.find(": warning ")
    if mark < 0 and not stripped.startswith("warning "):
        return None
    if mark >= 0:
        prefix = stripped[:mark]
        right = stripped[mark + len(": warning "):]
    else:
        prefix = ""
        right = stripped[len("warning "):]
    code, _, message = right.partition(": ")
    if len(code) < 2:
        return None
    file_path, line = location_of(prefix)
    if not file_path:
        file_path = "<solution>"
    code = code.upper()
    message = PROJECT_SUFFIX.sub("", message).strip()
    if not message:
        message = f"{code} emitted without a message"
    return {
        "code": code,
        "filePath": normalized_path(file_path),
        "lineNumber": line,
        "message": message,
    }


def key_of(warning: dict) -> tuple:
    return (warning["code"], warning["filePath"], warning["lineNumber"])


def dedupe(warnings: list[dict]) -> list[dict]:
    """Drop repeats of the same (code, file, line) — e.g. one per TFM."""
    seen: set = set()
    out: list[dict] = []
    for warning in warnings:
        key = key_of(warning)
        if key in seen:
            continue
        seen.add(key)
        out.append(warning)
    return out


def collect_warnings(output: str) -> list[dict]:
    return dedupe(sorted(
        (w for w in (parse_warning(line) for line in output.splitlines()) if w),
        key=key_of,
    ))


def write_artifacts(warnings: list[dict]) -> None:
    REPORT.write_text(json.dumps({"tool": "dotnet build", "warnings": warnings,
                                  "count": len(warnings)}, indent=2))
    QUEUE.write_text("\n".join(queue_lines(warnings)) + "\n")


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
    return [warning["code"], f"{rel}:{warning['lineNumber'] or '?'}", warning["message"]]


def queue_rows(warnings: list[dict]) -> list[list[str]]:
    rows = [["Code", "Location", "Message"],
            ["---", "---", "---"]]
    for warning in warnings:
        rows.append(row_for(warning))
    return rows


def code_counts(warnings: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for warning in warnings:
        counts[warning["code"]] = counts.get(warning["code"], 0) + 1
    return counts


def counts_text(warnings: list[dict]) -> str:
    counts = code_counts(warnings)
    if not counts:
        return "no warnings"
    return ", ".join(f"{code} x{n}" for code, n in sorted(counts.items()))


def queue_lines(warnings: list[dict]) -> list[str]:
    return [
        render_table(queue_rows(warnings)),
        "",
        f"**Gate**: {len(warnings)} warning(s) found — 0 required (clean build). "
        f"Full data in warnings-report.json.",
        "",
        "Suppress (repo policy): a *specific* `<NoWarn>` entry in the csproj with a documented reason. "
        "Blanket suppression to dodge the gate is rejected (anti-gaming rules).",
        "",
        "Diagnosis (verifier): read the source at each location and add a fix recommendation.",
        "Fix (implementor): fix the warning — remove the unused code, apply the analyzer's suggested "
        "API — or add the specific NoWarn with a reason; then re-run this audit.",
    ]


def print_summary(warnings: list[dict], build_ok: bool) -> None:
    files = {w["filePath"] for w in warnings}
    print()
    print(f"Build: {'OK' if build_ok else 'FAILED'}")
    print(f"Warnings: {len(warnings)} in {len(files)} file(s) — {counts_text(warnings)}")
    print(f"Reports: {REPORT.name}, {QUEUE.name}")


def gate_exit(warnings: list[dict]) -> int:
    if warnings:
        print(f"==> FAIL: {len(warnings)} warning(s) need work (see {QUEUE})")
        return 1
    print("==> PASS: clean build (0 warnings)")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-gate", action="store_true", help="run and report, but always exit 0")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    proc = run_build(solution_path())
    build_ok = proc.returncode == 0
    warnings = collect_warnings(proc.stdout)
    write_artifacts(warnings)
    print_summary(warnings, build_ok)
    if not build_ok:
        print(f"==> BUILD FAILED (exit {proc.returncode}) — tail:")
        print_tail(proc)
        return 0 if args.no_gate else 2
    return 0 if args.no_gate else gate_exit(warnings)


if __name__ == "__main__":
    sys.exit(main())