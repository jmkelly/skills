# quality-loop-skill

Deterministic quality gates for **.NET** and **Python** repositories — CRAP < 10,
metrics (radon / Dependably.CodeMetrics), zero build warnings, Stryker mutation testing — plus a
two-agent loop that drives the gates green. Distributed as a skill for the
**pi** LLM harness, but every script runs standalone from the CLI.

## What this is, for a maintainer

Every codebase drifts: methods grow, coverage thins, and "we'll clean it up
later" never comes. This project turns that drift into **deterministic,
machine-checkable gates** that both CI and coding agents can act on:

- **Per-function/per-method findings, not vibes.** Each audit walks the source
  and reports exactly which `file:line` is over the threshold, how far over
  (CRAP value, complexity, coverage), and why (rule name). No "this file looks
  complex" — a list of offenders, worst first, in a markdown queue.
- **Same input ⇒ same result.** Audits are pure functions of source + tool
  version + config. That makes them safe to run in CI (no flaky gates) *and*
  safe to hand to an agent fix-loop (the verdict doesn't change under the
  agent's feet).
- **The gate is the fix strategy.** The primary lever is *reducing complexity*
  (extracting methods, guard clauses, table-driven logic), not plastering over
  coverage — anti-gaming rules in [`SKILL.md`](SKILL.md) reject
  `[ExcludeFromCodeCoverage]`, `# pragma: no cover`, swallowed exceptions, and
  line-shuffling "refactors".
- **One loop for both stacks.** Stack is auto-detected (`*.sln`/`*.csproj` →
  .NET, `pyproject.toml`/`setup.py`/`requirements.txt` → Python), same gate
  semantics, same queue/report JSON schema, so your workflow is identical
  either way.

## How it works

| Concept | What it is |
|---|---|
| Gate | An audit script + a rule (e.g. "CRAP < 10 per method"). Exits 0 when green, 1 when red. |
| Queue | `crap-queue.md` / `metrics-queue.md` / `warnings-queue.md` / `stryker-queue.md` written to the repo root — offenders, worst first, with `file:line`. |
| Loop | Runs cheap gates before expensive ones, hands the worst offenders to a headless implementor (`pi -p`, fresh session per pass), re-audits, repeats until every gate exits 0. |

A gate is **red** while its queue lists offenders; the loop finishes only when
every audit exits 0 and every queue is empty.

Full semantics, exit codes, anti-gaming rules, and known-tooling notes:
[`SKILL.md`](SKILL.md). Role definitions: [verifier](references/quality-verify.md),
[implementor](references/quality-fix.md).

---

## .NET

| Gate | Tool | Rule |
|---|---|---|
| quality | `crap4dotnet` (CRAP = complexity² × (1 − coverage) + complexity) | **CRAP < 10** per method, test project excluded |
| metrics | `Dependably.CodeMetrics` (Roslyn) | `.dependably` rules: MI ≥ 20, cyclomatic ≤ 25, … |
| warnings | `dotnet build --no-incremental` | **zero build warnings** (CS/analyzer/NU/MSB) |
| mutation | `dotnet-stryker` | `thresholds.break` |

**Setup** — `dotnet` plus three global tools:

```bash
dotnet tool install -g crap4dotnet
dotnet tool install --global Dependably.CodeMetrics
dotnet tool install --global dotnet-stryker
```

Plus the repo's test deps. All audit scripts are skill-local and
repo-agnostic; the repo carries only *policy* files: `.dependably` at the repo
root (rules/excludes/grandfathered exceptions — or the skill-bundled default),
`stryker-config.json` in the test project (project under test, thresholds; the
audit generates a pinned default otherwise), and project-level `<NoWarn>`
entries (warning suppression — a specific code with a documented reason;
blanket `NoWarn` is rejected).

**Run** — CRAP audit (~1 min, includes `dotnet test`):

```bash
python3 scripts/dotnet/audit.py                 # dotnet test + crap4dotnet
python3 scripts/dotnet/audit.py --skip-tests    # reuse coverage.cobertura.xml (stale)
python3 scripts/dotnet/audit.py --include-tests # also gate the test project
```

Metrics (~10 s) and mutation (~11 min — expensive, the loop only pays it when
quality, metrics, and warnings are already green):

```bash
python3 scripts/dotnet/metrics-audit.py
python3 scripts/dotnet/warnings-audit.py   # non-incremental build, zero warnings
python3 scripts/dotnet/stryker-audit.py
```

**Notes**

- `crap4dotnet` targets net8 while installed runtimes are 9/10; the audits set
  `DOTNET_ROLL_FORWARD=LatestMajor` for it and the stryker testhost.
- The warnings audit builds with `--no-incremental` (up-to-date projects can't
  hide warnings) and `DOTNET_CLI_UI_LANGUAGE=en` (localized SDKs parse
  identically). NUxxxx NuGet-audit warnings follow feed advisory data, the one
  non-byte-deterministic class — treat via `<NoWarn>` policy if they are noise.
- `Program.<Main>$` entries are compiler-merged top-level statements of
  `Program.cs` — fix by moving statements into named static methods, not by
  renaming.
- `COVERAGE_STALE` warnings (Razor-generated entries, ~71% unmatched) are known
  noise: coverage is matched via PDB sequence points; trust the per-method
  numbers in `crap-report.json`, not the warning.

---

## Python

| Gate | Tool | Rule |
|---|---|---|
| quality | radon cc × coverage.py (same CRAP formula, per-function) | **CRAP < 10** per function, `tests/` excluded |
| metrics | radon (module MI, function cc, arg count) | MI ≥ 20, cc ≤ 25, args ≤ 7 |
| warnings | `pyflakes` | **zero findings** (unused imports, undefined names) |

Python has **no mutation gate yet** (no Stryker equivalent in this skill) —
the loop driver simply doesn't offer `--skip stryker` for Python repos.

**Setup**

```bash
pip install radon coverage pyflakes    # plus the repo's test deps (e.g. pytest)
```

**Run** — CRAP audit (runs `coverage run -m pytest -q`):

```bash
python3 scripts/python/audit.py                 # coverage + radon CRAP audit
python3 scripts/python/audit.py --skip-tests    # reuse artifacts/coverage.json (stale)
python3 scripts/python/audit.py --include-tests # also gate the tests/ dir
```

Metrics:

```bash
python3 scripts/python/metrics-audit.py         # mirrors the .dependably rules
python3 scripts/python/warnings-audit.py        # pyflakes, zero findings
```

Both write `crap-report.json` / `metrics-report.json` + queues in the same
schema/shape as the .NET audits — `jq` queries work unchanged across stacks
(`.namespace` is the dotted module path for Python).

**Notes**

- Per-function CRAP coverage counts the function's AST line span against
  coverage.py `executed_lines`; `# pragma: no cover` lines don't count against
  it.
- MI is module-level (that's how radon computes it); per-function slices are
  unreliable (radon 6 zeroes Halstead volume on calls/`with`). Pin tool
  versions: `radon`, `coverage`, `pyflakes`, plus the test runner — the loop's
  determinism depends on it.

---

## Using it from the pi harness

This is installed as a **pi skill** at `~/.pi/agent/skills/quality-loop/`, so
pi loads it automatically when a task matches its description (audits, queues,
CRAP, Stryker, verifier/implementor work):

- **One-shot**: ask your agent to *"run the quality loop for this repo"* — it
  reads `SKILL.md`, runs the audits for the detected stack, and drives the
  fixes. Or ask for a single audit: *"run the CRAP audit"*.
- **Automated loop**: `scripts/quality-loop.py [max-iterations] [batch-size]
  [--skip <audit> ...] [--dry-run]` — headless implementor passes via `pi -p`
  (fresh session per pass, unique session dir per run, one-paragraph handoff
  between passes). `--dry-run` audits and prints the implementor brief without
  launching pi. Env overrides: `QUALITY_MODEL`, `QUALITY_SESSION_DIR`,
  `QUALITY_PI_APPROVE=0`.
- **Manual two-agent pass**: the verifier (never edits code) runs every audit,
  diagnoses each offender near `file:line`, and records fix recommendations in
  the queues; the implementor fixes worst-first and keeps the build green.
  Repeat until every audit exits 0.
- Because the skill is fully contained, nothing is copied into the audited
  repo — only the policy files (`.dependably`, `stryker-config.json`) live
  there. Works standalone too, from any directory inside the repo:

```bash
python3 ~/.pi/agent/skills/quality-loop/scripts/quality-loop.py --dry-run
```

## Developing / testing

```bash
pip install -e .[dev]   # pytest
pytest
```

310 tests cover the audits' discovery, analysis, report schemas, and the loop
driver's stack detection and iteration logic.

## License

MIT — see [LICENSE](LICENSE).