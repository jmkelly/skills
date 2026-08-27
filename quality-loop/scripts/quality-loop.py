#!/usr/bin/env python3
"""Two-agent quality loop (language-aware).

Detects the repo stack (.NET or Python) and runs the matching deterministic
gates:

  .NET:
    quality   scripts/dotnet/audit.py                     gate CRAP < 10            -> crap-queue.md
    coverage  scripts/dotnet/coverage-audit.py             gate authored branch >= floor -> coverage-queue.md
    metrics   scripts/dotnet/metrics-audit.py             gate .dependably          -> metrics-queue.md
    warnings  scripts/dotnet/warnings-audit.py            gate zero build warnings  -> warnings-queue.md
    stryker   scripts/dotnet/stryker-audit.py             gate thresholds.break     -> stryker-queue.md

All .NET audits are skill-local and repo-agnostic (repo/solution/test-project
discovery); repos carry only *policy* files: a `.dependably` at the repo root
(rules/excludes/exceptions), a `stryker-config.json` in the test project
(project under test, thresholds), a `coverage-policy.json` at the repo root
(branch floor / queue size), and project-level `<NoWarn>` entries
(warning suppression). See SKILL.md for the bundled default configs.

  Python:
    quality   scripts/python/audit.py                     gate CRAP < 10       -> crap-queue.md
    metrics   scripts/python/metrics-audit.py             gate radon rules     -> metrics-queue.md
    warnings  scripts/python/warnings-audit.py            gate zero pyflakes findings -> warnings-queue.md
                                                          (mutation testing / Stryker: not yet available for Python)

Each iteration:
  1. VERIFIER (deterministic): cheap gates first — quality (CRAP, also
     regenerates the coverage data), then coverage (parses the fresh
     cobertura file, no extra test run), then metrics, then warnings
     (non-incremental .NET build / pyflakes scan).
     Stryker (mutation testing, ~11 min full run) only runs in
     iterations where quality, metrics AND warnings pass, so an iteration
     never pays the Stryker cost while a cheaper gate is already failing.
     Each audit gates on its own rules and writes its own work queue.
  2. If any gate failed: IMPLEMENTOR — a headless pi session fixes the worst
     `batch-size` offenders across the failing queues. **Each pass starts a
     fresh session**; past session files accumulate in the session dir for
     forensics or takeover. A one-paragraph handoff summary is carried from
     each pass to the next via `<session-dir>/implementor-summary.txt`, so a
     fresh session still knows what the previous one did. The default session
     dir is UNIQUE per loop run (repo slug + start timestamp, see
     session_dir_from_env), so runs of different repos — or later runs of the
     same repo — never inherit stale handoff state; QUALITY_SESSION_DIR pins
     an exact dir instead.
  3. Re-audit, repeat until every enabled gate passes.

Audits are deterministic — same source, tool version, and config ⇒ same
result; pin tool versions (see SKILL.md Notes). A skipped audit
(--skip) counts as passing for prerequisite ordering.

Usage: quality-loop.py [max-iterations] [batch-size] [--skip <audit> ...] [--dry-run]

  --dry-run   run the audit phase and print the implementor brief without
              launching a pi session (no fixes are made)

This driver is packaged as part of the quality-loop pi skill (skill root =
the directory containing SKILL.md); it can be run directly with python3
from anywhere inside the repo (it locates the repo root via git) or the agent
runs it when following the skill.

Environment (QUALITY_* names):
  QUALITY_MODEL         model for the implementor pi call (default: pi's default)
  QUALITY_SESSION_DIR   implementor session dir (default: ~/.pi/sessions/quality-implementor)
  QUALITY_PI_APPROVE    "0" to skip -a (project-local file approval) on pi calls
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
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
    # Spawned by the pi skill with cwd = current project; also usable
    # standalone from inside the repo. Repo root = git toplevel from the
    # process cwd, then from the script dir (legacy layouts), else cwd.
    return git_root_or(Path.cwd(), Path(__file__).resolve().parent) or Path.cwd()


REPO = find_repo()
LOOP_DIR = Path(__file__).resolve().parent
HANDOFF_FILE = "implementor-summary.txt"

DOTNET_DATA_FILES = "crap-report.json / coverage-report.json / metrics-report.json / warnings-report.json / StrykerOutput/<latest>/reports/mutation-report.json"
DOTNET_TEST_CMD = "run the full test suite (dotnet test; Testcontainers needs Docker)"
PYTHON_DATA_FILES = "crap-report.json / metrics-report.json / warnings-report.json"
PYTHON_TEST_CMD = "run the full test suite (pytest)"

DOTNET_GUIDANCE = {
    "quality": (
        "crap-queue.md: reduce complexity (extract methods, guard clauses, move logic to "
        "services) as the primary lever; add REAL unit tests only when that is the cheap "
        "lever for low-complexity untested methods. Never add ExcludeFromCodeCoverage, "
        "pragmas, or fake tests."
    ),
    "coverage": (
        "coverage-queue.md: add REAL unit/integration tests for untested methods, most uncovered "
        "lines first; mocks/smoke tests only where the method is I/O-bound. Generated code "
        "(.cshtml, Migrations/, compiler-generated classes) is excluded automatically — never "
        "game the denominator or add ExcludeFromCodeCoverage."
    ),
    "metrics": (
        "Metrics queue: raise Maintainability Index / lower cyclomatic, cognitive, nesting, coupling, LCOM4. "
        "Use .dependably `exceptions` ONLY to grandfather a deliberate accept (with reason + expiry), never "
        "to silence work. Generated migrations are already excluded."
    ),
    "stryker": (
        "Stryker queue: surviving mutants mean tests that don't detect faults — add or tighten the test that "
        "should kill each mutant (or refactor if the mutation exposes dead code). Mutants in untested CLI "
        "strings may indicate missing tests for that surface, not string tweaks."
    ),
    "warnings": (
        "warnings-queue.md: fix the warning — remove the unused code, apply the analyzer's suggested API. "
        "A *specific* csproj <NoWarn> entry with a documented reason is legitimate repo policy; blanket "
        "NoWarn to dodge the gate is not (anti-gaming rules)."
    ),
}

PYTHON_GUIDANCE = {
    "quality": (
        "crap-queue.md: reduce complexity (extract functions, guard clauses, table-driven "
        "dispatch) as the primary lever; the audit computes CRAP from radon complexity and "
        "coverage.py per-function coverage. Add REAL tests only when that is the cheap lever "
        "for low-complexity untested functions. Never add '# pragma: no cover' to hide work."
    ),
    "metrics": (
        "Metrics queue: raise Maintainability Index / lower cyclomatic complexity and "
        "parameter counts (radon). No per-file suppressions; refactor honestly."
    ),
    "warnings": (
        "warnings-queue.md (pyflakes): remove the unused import/variable or define the missing name. "
        "pyflakes has no suppression mechanism — fix, don't silence."
    ),
}


@dataclass(frozen=True)
class ImplementorConfig:
    stack: str
    batch: int
    session_dir: Path
    model: str
    approve: bool
    auds: dict


def dotnet_marker(repo: Path) -> bool:
    return any(repo.glob("*.sln")) or any(repo.glob("*.csproj"))


def setup_files(repo: Path) -> bool:
    return any(repo.glob("setup.py")) or any(repo.glob("setup.cfg"))


def python_project_files(repo: Path) -> bool:
    return setup_files(repo) or any(repo.glob("pyproject.toml"))


def python_marker(repo: Path) -> bool:
    return python_project_files(repo) or (repo / "requirements.txt").exists()


def python_or_error(repo: Path) -> str:
    if python_marker(repo):
        return "python"
    raise SystemExit(
        "ERROR: cannot detect the project stack in the repo root "
        f"({repo}) — expected a .NET (sln/csproj) or Python (pyproject.toml/setup.py/requirements.txt) project."
    )


def detect_stack(repo: Path) -> str:
    """dotnet | python | raises SystemExit when neither is detectable."""
    if dotnet_marker(repo):
        return "dotnet"
    return python_or_error(repo)


def build_audits(stack: str) -> dict:
    """audit name -> (script path, queue file, human description, prerequisites)
    Prerequisites: audits that must have PASSED in this iteration before this
    one runs (a prerequisite that is skipped counts as passing)."""
    if stack == "dotnet":
        return {
            "quality": (
                LOOP_DIR / "dotnet" / "audit.py",
                "crap-queue.md",
                "CRAP < 10 for every method",
                (),
            ),
            "coverage": (
                LOOP_DIR / "dotnet" / "coverage-audit.py",
                "coverage-queue.md",
                "authored branch coverage >= coverage-policy.json floor (default 70%)",
                ("quality",),
            ),
            "metrics": (
                LOOP_DIR / "dotnet" / "metrics-audit.py",
                "metrics-queue.md",
                ".dependably metric rules (MI >= 20, cyclomatic <= 25, ...)",
                (),
            ),
            "warnings": (
                LOOP_DIR / "dotnet" / "warnings-audit.py",
                "warnings-queue.md",
                "zero build warnings (dotnet build --no-incremental)",
                (),
            ),
            "stryker": (
                LOOP_DIR / "dotnet" / "stryker-audit.py",
                "stryker-queue.md",
                "mutation score >= thresholds.break",
                ("quality", "metrics", "warnings"),
            ),
        }
    return {  # python
        "quality": (
            LOOP_DIR / "python" / "audit.py",
            "crap-queue.md",
            "CRAP < 10 for every function (radon cc x coverage.py)",
            (),
        ),
        "metrics": (
            LOOP_DIR / "python" / "metrics-audit.py",
            "metrics-queue.md",
            "radon rules (MI >= 20, cyclomatic <= 25, args <= 7)",
            (),
        ),
        "warnings": (
            LOOP_DIR / "python" / "warnings-audit.py",
            "warnings-queue.md",
            "zero pyflakes findings",
            (),
        ),
    }


def unknown_skips(skip: list[str], auds: dict) -> list[str]:
    return list(filter(lambda s: s not in auds, skip))


def validate_skips(skip: list[str], auds: dict, stack: str) -> None:
    unknown = unknown_skips(skip, auds)
    if unknown:
        raise SystemExit(f"ERROR: --skip {', '.join(unknown)} — no such audit for a {stack} repo "
                         f"(available: {', '.join(auds)})")


def enabled_audits(skip: list[str], auds: dict) -> list[str]:
    return list(filter(lambda name: name not in skip, auds))


def require_enabled(enabled: list[str]) -> list[str]:
    if not enabled:
        raise SystemExit("ERROR: all audits skipped — nothing to do.")
    return enabled


def session_dir_from_env() -> Path:
    """Implementor session dir.

    QUALITY_SESSION_DIR, when set, is the exact dir (escape hatch for CI and
    forensics). The default is a UNIQUE per-run dir under the base:
    `~/.pi/sessions/quality-implementor/<repo-slug>-<timestamp>`. Handoff and
    session JSONLs therefore only ever span passes of one loop invocation —
    runs of different repos (or later runs of the same repo) can never leak
    stale handoff state into each other. The queues regenerated by each audit
    are the source of truth; the handoff only carries within-run context.
    """
    override = os.environ.get("QUALITY_SESSION_DIR")
    if override:
        return Path(override)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", REPO.name).strip("-") or "repo"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path.home() / ".pi" / "sessions" / "quality-implementor" / f"{slug}-{stamp}"


def wrap_previous(prev: str) -> str:
    if not prev:
        return ""
    return (
        "\nHandoff from the previous implementor pass (summary of what was done and is in flight):\n"
        "----- previous pass summary -----\n"
        f"{prev[:4000]}"
        "\n----- end previous pass summary -----\n"
    )


def previous_handoff(handoff: Path) -> str:
    if not handoff.exists():
        return ""
    return wrap_previous(handoff.read_text(encoding="utf-8").strip())


def queue_links(failed: list[str], auds: dict) -> str:
    return "\n".join(f"- {auds[name][1]} (failing gate: {auds[name][2]})" for name in failed)


def guidance_lines(failed: list[str], guidance: dict) -> list[str]:
    return [f"- {guidance[name]}" for name in failed]


def guidance_for(stack: str) -> tuple[str, str, dict]:
    if stack == "dotnet":
        return (DOTNET_DATA_FILES, DOTNET_TEST_CMD, DOTNET_GUIDANCE)
    return (PYTHON_DATA_FILES, PYTHON_TEST_CMD, PYTHON_GUIDANCE)


def model_arg(model: str) -> list[str]:
    return ["--model", model] if model else []


def approve_arg(approve: bool) -> list[str]:
    return ["-a"] if approve else []


def build_pi_command(config: ImplementorConfig, brief: str) -> list[str]:
    cmd = ["pi"] + model_arg(config.model) + approve_arg(config.approve)
    cmd += ["--session-dir", str(config.session_dir)]
    cmd += ["--name", "quality-implementor"]  # fresh session per pass; old ones stay on disk
    cmd += ["-p", brief]
    return cmd


def run_pi(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, cwd=REPO, check=False)
    except FileNotFoundError:
        raise SystemExit("ERROR: 'pi' not found on PATH — is pi installed?\n"
                         "       Run the loop from an environment where `pi` is available (e.g. via mise shims).")


def note_missing_handoff(session_dir: Path) -> None:
    handoff = session_dir / HANDOFF_FILE
    if not handoff.exists():
        print(f"    NOTE: implementor did not write {handoff} — no handoff for the next pass.")


def launch(config: ImplementorConfig, cmd: list[str]) -> None:
    print(f"    pi command: {' '.join(cmd)}")
    run_pi(cmd)
    note_missing_handoff(config.session_dir)


def print_dry_run(brief: str) -> None:
    print("    [dry-run] would run the implementor; brief:")
    print("    " + brief.replace(chr(10), chr(10) + "    "))


def queue_order_names(auds: dict) -> str:
    return ", ".join(auds[name][1] for name in auds)


def build_brief(config: ImplementorConfig, failed: list[str]) -> str:
    handoff = config.session_dir / HANDOFF_FILE
    previous = previous_handoff(handoff)
    queues = queue_links(failed, config.auds)
    data_files, test_cmd, guidance = guidance_for(config.stack)
    lines = guidance_lines(failed, guidance)

    return f"""You are the implementor in the quality loop for this {config.stack} repo. The verifier's latest audit failed on these suites:
{queues}

{previous}
1. Read each listed queue file in the project root (worst offenders first); full data lives next to each:
   {data_files}.
2. Refactor the worst {config.batch} offenders this pass, worst-first across the failing queues in this order:
   {queue_order_names(config.auds)}
   (never skip a queue entirely if it has offenders and your batch isn't spent). This is a fresh
   session: previous passes' sessions (JSONL files) are in the session dir if you want to see what
   was already attempted, but the queues are regenerated by each audit and list only items still
   failing — trust them as the source of truth. The handoff summary above tells you what the
   previous pass did; don't redo finished work.
3. Per-suite guidance:
{chr(10).join(lines)}
4. Keep behavior identical; keep the build green and run targeted tests after each change.
5. Before finishing, {test_cmd}.
6. Finish by writing a short list (item, file:line, metric before -> estimated after, what you changed,
   plus anything left half-done) to {handoff}, overwriting it, and print it too. The loop reads that
   file into the next pass's brief. If every queue is already empty for your batch, say so.

Stop when done. The loop script re-runs all audits automatically and will come back to you if any gate still fails."""


def run_implementor(config: ImplementorConfig, failed: list[str], dry_run: bool = False) -> None:
    brief = build_brief(config, failed)
    if dry_run:
        print_dry_run(brief)
        return
    launch(config, build_pi_command(config, brief))


def blocking_prereqs(prereqs: tuple, failed: list[str]) -> list[str]:
    return list(filter(lambda p: p in failed, prereqs))


def print_outcome(name: str, queue: str, desc: str, failed: list[str], passed: bool) -> None:
    if passed:
        print(f"    {name}: PASS")
        return
    failed.append(name)
    print(f"    {name}: FAIL (gate: {desc}) -> see {queue}")


def run_audit_script(name: str, script: Path, queue: str, desc: str, failed: list[str]) -> None:
    print(f"  running {name}: {script.name}")
    result = subprocess.run([sys.executable, str(script)], cwd=REPO)
    print_outcome(name, queue, desc, failed, result.returncode == 0)


def run_audit(name: str, auds: dict, failed: list[str], skipped: list[str]) -> None:
    script, queue, desc, prereqs = auds[name]
    blockers = blocking_prereqs(prereqs, failed)
    if blockers:
        skipped.append(name)
        print(f"  skipping {name}: prerequisite gate failed ({', '.join(blockers)})")
        return
    run_audit_script(name, script, queue, desc, failed)


def verify_iteration(enabled: list[str], auds: dict) -> tuple[list[str], list[str]]:
    failed: list[str] = []
    skipped: list[str] = []
    for name in enabled:
        run_audit(name, auds, failed, skipped)
    return failed, skipped


def print_all_clean(enabled: list[str], auds: dict) -> None:
    print("\n==> ALL GATES PASSED")
    for name in enabled:
        print(f"    {auds[name][1]}: clean")


def queue_paths(enabled: list[str], auds: dict) -> str:
    return ", ".join(str(REPO / auds[name][1]) for name in enabled)


def print_exhausted(enabled: list[str], auds: dict, session_dir: Path, max_iterations: int) -> None:
    if any(session_dir.glob("*.jsonl")):
        print(f"==> Implementor sessions kept at: {session_dir}")
        print(f"    Take over the most recent pass with: pi -c --session-dir {session_dir}")
    print(f"==> Max iterations ({max_iterations}) reached without passing every gate.")
    print("    Inspect the queues: " + queue_paths(enabled, auds))


def exit_after(results: list[bool], enabled: list[str], config: ImplementorConfig,
               max_iterations: int) -> int:
    if passed_last(results):
        print_all_clean(enabled, config.auds)
        return 0
    print_exhausted(enabled, config.auds, config.session_dir, max_iterations)
    return 1


def passed_last(results: list[bool]) -> bool:
    if not results:
        return False
    return results[-1]


def print_skipped(skipped: list[str]) -> None:
    if skipped:
        print(f"    not audited this iteration (prerequisite failed): {', '.join(skipped)}")


def iteration(i: int, enabled: list[str], config: ImplementorConfig,
              args: argparse.Namespace) -> bool:
    print("\n" + "=" * 64)
    print(f"Iteration {i}/{args.max_iterations} — VERIFIER (deterministic audits)")
    print("=" * 64)
    failed, skipped = verify_iteration(enabled, config.auds)
    if not failed:
        # The pass summary is printed once by exit_after(); the iteration
        # itself only reports the per-audit PASS lines from verify_iteration.
        return True

    print("\n" + "=" * 64)
    print(f"Iteration {i}/{args.max_iterations} — IMPLEMENTOR (pi, batch of {config.batch})")
    print(f"    failing suites: {', '.join(failed)}")
    print_skipped(skipped)
    print("=" * 64)
    run_implementor(config, failed, dry_run=args.dry_run)
    return False


def iteration_stream(enabled: list[str], config: ImplementorConfig,
                     args: argparse.Namespace):
    for i in range(1, args.max_iterations + 1):
        yield iteration(i, enabled, config, args)


def run_iterations(enabled: list[str], config: ImplementorConfig,
                   args: argparse.Namespace):
    # Yield every iteration result, including the first passing one, then stop.
    # (takewhile(not_done, ...) would swallow the passing iteration and leave
    # exit_after with no True result -> the loop could never exit 0.)
    for result in iteration_stream(enabled, config, args):
        yield result
        if result:
            return


def run_loop(enabled: list[str], config: ImplementorConfig, args: argparse.Namespace) -> int:
    results = run_iterations(enabled, config, args)
    return exit_after(list(results), enabled, config, args.max_iterations)


def build_config(args: argparse.Namespace, stack: str, auds: dict) -> ImplementorConfig:
    return ImplementorConfig(
        stack=stack,
        batch=args.batch_size,
        session_dir=session_dir_from_env(),
        model=os.environ.get("QUALITY_MODEL", ""),
        approve=os.environ.get("QUALITY_PI_APPROVE", "1") != "0",
        auds=auds,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("max_iterations", nargs="?", type=int, default=10, help="max loop iterations (default: 10)")
    parser.add_argument("batch_size", nargs="?", type=int, default=5, help="items per implementor pass (default: 5)")
    parser.add_argument(
        "--skip", action="append", default=[], help="skip an audit (can be repeated): quality|coverage|metrics|warnings[|stryker]"
    )
    parser.add_argument("--dry-run", action="store_true", help="audit phase only; print brief, skip pi")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stack = detect_stack(REPO)
    auds = build_audits(stack)
    validate_skips(args.skip, auds, stack)
    enabled = require_enabled(enabled_audits(args.skip, auds))
    config = build_config(args, stack, auds)
    config.session_dir.mkdir(parents=True, exist_ok=True)

    print(f"Detected stack: {stack} ({', '.join(enabled)} audits enabled; "
          f"repo {REPO})")
    return run_loop(enabled, config, args)


if __name__ == "__main__":
    sys.exit(main())