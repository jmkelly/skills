#!/usr/bin/env python3
"""Coverage audit for the .NET solution — authored-code coverage gate.

Measures line/branch coverage of *authored* code only. Generated code is
excluded with hard-coded heuristics (no config, nothing to game):

  - Razor/views/components        filenames ending in .cshtml
  - EF migrations                 path segment Migrations/ (incl. *.Designer.cs, *ModelSnapshot.cs)
  - C#-compiler-generated         class names containing '<' (async state machines, lambdas)
  - build output                  path segment obj/

The gate: authored branch coverage >= branchFloor (repo policy file
coverage-policy.json at the repo root; default 70). Writes:

    coverage-report.json    stats (lines/branches, per project, per excluded category)
    coverage-queue.md       untested authored methods, worst first, with file:line
    coverage-history.csv    appended trend row per run (line%, branch%, queue size)

Exit code: 0 when the gate passes, 1 when authored branch coverage is below
the floor. The queue is the actionable list regardless of gate state — the
implementor works it to raise coverage.

Coverage data comes from the newest artifacts/test-results/coverage.cobertura.xml
— freshly written by the quality (CRAP) audit, so the loop runs this audit
right after quality for free. Run standalone, it reuses the newest file if
present and otherwise runs dotnet test itself.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

def git_root(start: Path) -> Path | None:
    try:
        return Path(subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=start, text=True,
            stderr=subprocess.DEVNULL).strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def git_root_or(first: Path, second: Path) -> Path | None:
    return git_root(first) or git_root(second)


REPO = git_root_or(Path.cwd(), Path(__file__).resolve().parent) or Path.cwd()
RESULTS_DIR = REPO / "artifacts" / "test-results"
REPORT = REPO / "coverage-report.json"
QUEUE = REPO / "coverage-queue.md"
HISTORY = REPO / "coverage-history.csv"
POLICY = REPO / "coverage-policy.json"

GENERATED_SUFFIXES = (".cshtml",)
GENERATED_SEGMENTS = ("Migrations", "obj")


def load_policy() -> dict:
    policy = {"branchFloor": 70, "queueLimit": 30}
    if POLICY.is_file():
        try:
            user = json.loads(POLICY.read_text())
            if not isinstance(user, dict):
                raise ValueError("policy file must contain a JSON object")
            policy.update(user)
        except (json.JSONDecodeError, ValueError) as e:
            raise SystemExit(f"ERROR: {POLICY} is not valid JSON: {e}")
    for key in ("branchFloor", "queueLimit"):
        if not isinstance(policy.get(key), (int, float)):
            raise SystemExit(f"ERROR: coverage-policy.json '{key}' must be a number")
    if not 0 < policy["branchFloor"] <= 100:
        raise SystemExit("ERROR: coverage-policy.json 'branchFloor' must be in (0, 100]")
    return policy


def is_generated_filename(filename: str) -> str | None:
    """Reason code when the file is generated output, else None."""
    parts = [p for p in Path(filename).parts if p]
    if any(seg in GENERATED_SEGMENTS for seg in parts):
        return "buildOutput" if "obj" in parts else "efMigrations"
    if Path(filename).suffix.lower() in GENERATED_SUFFIXES:
        return "razor"
    return None


def is_compiler_generated_class(name: str) -> bool:
    return "<" in name


def test_namespace() -> str:
    projects = sorted(REPO.rglob("*.Tests.csproj"), key=lambda p: (len(p.parts), str(p)))
    return projects[0].stem if projects else ""


def newest_coverage() -> Path | None:
    matches = list(RESULTS_DIR.rglob("coverage.cobertura.xml"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def run_tests() -> Path:
    project = test_project()
    print("==> No coverage file found — running dotnet test with coverage...")
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    proc = subprocess.run(
        ["dotnet", "test", str(project), "--collect:XPlat Code Coverage",
         "--results-directory", str(RESULTS_DIR), "-v", "quiet"],
        capture_output=True, text=True,
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-5:])
    if tail:
        print(tail)
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: dotnet test failed (exit {proc.returncode})")
    cov = newest_coverage()
    if cov is None:
        raise SystemExit(f"ERROR: dotnet test ran but produced no coverage.cobertura.xml in {RESULTS_DIR}")
    return cov


def test_project() -> Path:
    projects = (*REPO.glob("*.Tests.csproj"),
                *sorted(REPO.rglob("*.Tests.csproj"), key=lambda p: (len(p.parts), str(p))))
    if not projects:
        raise SystemExit("ERROR: no *.Tests.csproj found under the repo root")
    return projects[0]


def parse_condition(condition_coverage: str) -> tuple[int, int]:
    """condition-coverage attr -> (covered, valid). '100% (2/2)' or '100%'."""
    m = re.search(r"\((\d+)/(\d+)\)", condition_coverage or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    if condition_coverage == "100%":
        return 1, 1
    return 0, 1  # e.g. '50%' without counts: count the branch as uncovered


def extract(root: ET.Element, limit: int):
    """Per-method data: skip generated/compiler classes; per-file dedupe for authored lines."""
    test_ns = test_namespace()
    methods, authored_files, categories = [], {}, {}
    for pkg in root.findall("packages/package"):
        pkg_name = pkg.get("name") or ""
        if test_ns and (pkg_name == test_ns or pkg_name.startswith(test_ns + ".")):
            continue
        for cls in pkg.findall("classes/class"):
            name = cls.get("name") or ""
            filename = cls.get("filename") or ""
            reason = is_generated_filename(filename)
            cat = "compilerGenerated" if is_compiler_generated_class(name) else (reason or "authored")
            # Per-method queue entries.
            for method in cls.findall("methods/method"):
                lines = method.findall("lines/line")
                if not lines:
                    continue
                covered = any(int(ln.get("hits", "0")) > 0 for ln in lines)
                uncovered = sum(1 for ln in lines if int(ln.get("hits", "0")) == 0)
                complexity = int(method.get("complexity") or 1)
                line_no = int(lines[0].get("number", "0"))
                entry = {
                    "filePath": str((REPO / filename).resolve()),
                    "lineNumber": line_no,
                    "className": name.rsplit("/", 1)[-1].rsplit(".", 1)[-1] if cat == "authored" else name,
                    "methodName": method.get("name", "?"),
                    "complexity": complexity,
                    "covered": covered,
                    "uncoveredLines": uncovered,
                    "category": cat,
                }
                if cat == "authored":
                    methods.append(entry)
                else:
                    categories.setdefault(cat, []).append(entry)
            # Line/branch totals, per CLASS (a file can mix authored and
            # generated classes, e.g. Program.cs); keep file-level dedupe for
            # authored lines because one source line may appear in several
            # method entries of the same class.
            totals = [0, 0, 0, 0]  # lines covered, valid, branches covered, valid
            outer = cls.findall("lines/line")
            for ln in outer:
                totals[1] += 1
                if int(ln.get("hits", "0")) > 0:
                    totals[0] += 1
                if ln.get("branch") == "True":
                    c, v = parse_condition(ln.get("condition-coverage", ""))
                    totals[2] += c
                    totals[3] += v
            if cat == "authored":
                fs = authored_files.setdefault(filename, {"lines": {}, "branches": {}})
                for ln in outer:
                    n = int(ln.get("number"))
                    fs["lines"][n] = max(fs["lines"].get(n, 0), int(ln.get("hits", "0")))
                    if ln.get("branch") == "True":
                        prev = fs["branches"].get(n, (0, 0))
                        cur = parse_condition(ln.get("condition-coverage", ""))
                        if cur[1] >= prev[1] and (cur[0] > prev[0] or cur[1] > prev[1]):
                            fs["branches"][n] = cur
            else:
                categories.setdefault(cat + "::totals", [0, 0, 0, 0])
                for i in range(4):
                    categories[cat + "::totals"][i] += totals[i]
    methods.sort(key=lambda m: (-m["uncoveredLines"], -m["complexity"], m["lineNumber"]))
    return methods[:limit], methods, authored_files, categories


def summarize(authored_files: dict, categories: dict) -> dict:
    tot = {"lines": [0, 0], "branches": [0, 0]}
    per_project = {}
    for filename, fs in authored_files.items():
        lc = sum(1 for h in fs["lines"].values() if h > 0)
        lv = len(fs["lines"])
        bc = sum(c for c, _ in fs["branches"].values())
        bv = sum(v for _, v in fs["branches"].values())
        project = Path(filename).parts[0] if Path(filename).parts else "?"
        per_project.setdefault(project, {"lines": [0, 0], "branches": [0, 0]})
        for bucket in (per_project[project], tot):
            bucket["lines"][0] += lc
            bucket["lines"][1] += lv
            bucket["branches"][0] += bc
            bucket["branches"][1] += bv

    def pct(c, v):
        return round(100.0 * c / v, 1) if v else None

    shape = lambda b: {"covered": b["lines"][0], "valid": b["lines"][1], "pct": pct(*b["lines"]),
                       "branchesCovered": b["branches"][0], "branchesValid": b["branches"][1],
                       "branchPct": pct(*b["branches"])}
    per_cat = {"authored": shape(tot)}
    for cat, entries in sorted(categories.items()):
        if cat.endswith("::totals"):
            t = entries
            per_cat[cat[:-8]] = shape({"lines": [t[0], t[1]], "branches": [t[2], t[3]]})
    return {"total": shape(tot),
            "perCategory": per_cat,
            "perProject": {p: shape(b) for p, b in sorted(per_project.items())}}


def queue_table(queue: list[dict]) -> str:
    rows = [["Cx", "UncovLn", "Method", "Location"],
            ["---:", "---:", "---", "---"]]
    for m in queue:
        rows.append([
            str(m["complexity"]), str(m["uncoveredLines"]),
            f"{m['className']}.{m['methodName']}",
            f"{m['filePath'].replace(str(REPO) + '/', '')}:{m['lineNumber']}",
        ])
    widths = [max(len(r[c]) for r in rows) for c in range(4)]
    return "\n".join("| " + " | ".join(row[c].ljust(widths[c]) for c in range(4)) + " |" for row in rows)


def write_outputs(stats: dict, queue: list[dict], all_authored: list[dict], policy: dict,
                  cov: Path, passed: bool) -> None:
    total = stats["total"]
    branch = total["branchPct"] or 0.0
    untested = sum(1 for m in all_authored if not m["covered"])
    uncovered_lines = sum(m["uncoveredLines"] for m in all_authored if not m["covered"])
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coverageFile": str(cov),
        "policy": policy,
        "stats": stats,
        "gate": {
            "rule": f"authored branch coverage >= {policy['branchFloor']}%",
            "branchCoverage": branch,
            "passed": passed,
            "untestedAuthoredMethods": untested,
            "uncoveredAuthoredLines": uncovered_lines,
        },
        "queue": queue,
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n")

    lines = [
        f"**Authored-code coverage**: {total['pct']}% lines  {branch}% branches  "
        f"({total['covered']}/{total['valid']} lines)",
        f"**Queue**: {untested} untested authored methods, {uncovered_lines} uncovered lines "
        f"(worst {len(queue)} below; full list in coverage-report.json).",
        "",
        queue_table(queue),
        "",
        f"**Gate**: authored branch coverage {branch}% >= {policy['branchFloor']}% "
        f"({POLICY.relative_to(REPO) if POLICY.is_file() else 'default policy'}) -> {'PASS' if passed else 'FAIL'}",
        "",
        "Generated code is excluded (hard-coded): .cshtml, Migrations/, compiler-generated "
        "async/lambda classes, obj/. Trend: coverage-history.csv.",
        "",
        "Fix (implementor): add REAL unit/integration tests for the untested methods at the "
        "top of this queue (most uncovered lines first) — mocks/smoke tests only where the "
        "method is I/O-bound. Never ExcludeFromCodeCoverage or fake tests.",
    ]
    QUEUE.write_text("\n".join(lines) + "\n")

    with HISTORY.open("a", newline="") as fh:
        writer = csv.writer(fh)
        if HISTORY.stat().st_size == 0:
            writer.writerow(["timestamp", "linePct", "branchPct", "untestedMethods",
                             "uncoveredLines", "branchFloor", "passed"])
        writer.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         total["pct"], branch, untested, uncovered_lines,
                         policy["branchFloor"], passed])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, help="explicit coverage.cobertura.xml path")
    parser.add_argument("--skip-tests", action="store_true",
                        help="reuse newest coverage, do not run dotnet test (stale; experiments only)")
    parser.add_argument("--no-gate", action="store_true",
                        help="report and queue only; always exit 0")
    args = parser.parse_args()

    policy = load_policy()
    if args.coverage:
        cov = args.coverage
    elif args.skip_tests or newest_coverage():
        cov = newest_coverage()
        if cov:
            print(f"==> Reusing newest coverage: {cov}")
    else:
        cov = run_tests()

    print(f"==> Parsing {cov} (policy: {POLICY.name if POLICY.is_file() else 'defaults'})")
    root = ET.parse(str(cov)).getroot()
    queue, all_authored, authored_files, categories = extract(root, int(policy["queueLimit"]))
    stats = summarize(authored_files, categories)
    total = stats["total"]
    branch = total["branchPct"] or 0.0
    passed = args.no_gate or branch >= policy["branchFloor"]
    write_outputs(stats, queue, all_authored, policy, cov, passed)

    print(f"==> Authored code: {total['pct']}% lines, {branch}% branches "
          f"({total['covered']}/{total['valid']} lines, "
          f"{total['branchesCovered']}/{total['branchesValid']} branches)")
    for cat, s in stats["perCategory"].items():
        if cat != "authored":
            print(f"    excluded '{cat}': {s['pct']}% lines, {s['branchPct']}% branches")
    print(f"==> Queue: {sum(1 for m in all_authored if not m['covered'])} untested authored methods "
          f"(top {len(queue)} in {QUEUE}); trend -> {HISTORY}")
    if passed:
        print(f"==> PASS: authored branch coverage {branch}% >= {policy['branchFloor']}%")
        return 0
    print(f"==> FAIL: authored branch coverage {branch}% < {policy['branchFloor']}% (see {QUEUE})")
    return 1


if __name__ == "__main__":
    sys.exit(main())