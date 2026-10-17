#!/usr/bin/env python3
"""Deterministic code-metrics audit for a .NET solution — the verifier half of
the two-agent loop.

Runs `codemetrics` (Dependably.CodeMetrics — the Linux-native, Roslyn-based
successor to the .NET Framework-only Microsoft.CodeAnalysis.Metrics.exe) over
the repo, then:
  - writes metrics-report.json (full codemetrics JSON output)
  - writes metrics-queue.md (worst offenders, worst-first — same shape as
    crap-queue.md so a fix loop can consume it)
  - exit 0 = gate passed; exit 1 = a rule/failOn gate tripped

Gate rules come from `.dependably` in the repo root when the repo declares one
(repo policy: thresholds, excludes, grandfathered exceptions). Without one,
this audit falls back to the skill-bundled default config
(`<script dir>/.dependably.default` — generic thresholds only, no exceptions).

Determinism: given the same source, tool version, and config file the report is
byte-identical. Pin the tool version (see SKILL.md).

Usage: metrics-audit.py [--no-gate]

  --no-gate   run the scan and write reports, but always exit 0 (codemetrics'
              own exit code is otherwise passed through: 1 = a rule/failOn gate
              tripped, 2 = usage error).
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = "codemetrics"
DEFAULT_CONFIG_NAME = ".dependably.default"

# .dependably rule -> (metrics section, field, direction, human noun). codemetrics
# counts a rule breach toward its gate exit code but (0.1.2) does not emit
# lcom4/coupling/nesting breaches in the JSON `findings` array — it reports
# `findings: 0` while exiting 1 with `GateBreaches: N`. These mappings let the
# audit re-derive the same breaches from the raw per-method/per-type metrics so
# the queue is never red-and-empty. `coupling` is the in-repo fan-out (the same
# value the tool's `hub`/god-class diagnosis uses), not total class coupling.
RULE_METRICS: dict[str, tuple[str, str, str]] = {
    "cyclomatic": ("Methods", "Cyclomatic", "max"),
    "cognitive": ("Methods", "Cognitive", "max"),
    "nesting": ("Methods", "MaxNesting", "max"),
    "mi": ("Methods", "MaintainabilityIndex", "min"),
    "lcom4": ("Types", "Lcom4", "max"),
    "coupling": ("Types", "InRepoCoupling", "max"),
}

RULE_TEXT: dict[str, tuple[str, str]] = {
    "cyclomatic": ("Complex method: cyclomatic {value} (max {limit}) for {name}.",
                   "Extract helper methods to reduce cyclomatic complexity."),
    "cognitive": ("High cognitive complexity: {value} (max {limit}) for {name}.",
                  "Extract helpers and flatten nested branching."),
    "nesting": ("Deep nesting: {value} (max {limit}) for {name}.",
                "Use guard clauses or extract nested logic."),
    "mi": ("Low maintainability index: {value} (min {limit}) for {name}.",
           "Split the method / reduce its complexity."),
    "lcom4": ("Low cohesion: LCOM4 {value} (max {limit}) for {name}.",
              "Split responsibilities into focused types."),
    "coupling": ("High in-repo coupling: {value} (max {limit}) for {name}.",
                 "Reduce fan-out; extract collaborators behind a narrow seam."),
}

SEVERITIES = ("critical", "high", "moderate", "low", "info")
SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}


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
REPORT = REPO / "metrics-report.json"
QUEUE = REPO / "metrics-queue.md"
DEFAULT_CONFIG = Path(__file__).resolve().parent / DEFAULT_CONFIG_NAME


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path.home() / ".dotnet" / "tools" / name
    return str(candidate) if candidate.exists() else None


def gate_config() -> Path:
    """Repo `.dependably` when the repo declares one, else the bundled default."""
    repo_config = REPO / ".dependably"
    if repo_config.exists():
        return repo_config
    if not DEFAULT_CONFIG.exists():
        raise SystemExit(f"ERROR: bundled default config {DEFAULT_CONFIG} is missing")
    return DEFAULT_CONFIG


def severity_for(config_severity: str) -> str:
    """Map a `.dependably` rule severity to the shared finding-severity scale."""
    return {"error": "high", "warn": "moderate", "warning": "moderate"}.get(config_severity, "moderate")


def load_config(config: Path) -> dict:
    try:
        return json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_rules(config: Path) -> dict[str, dict]:
    """The enabled `.dependably` rules: name -> {severity, max|min}."""
    data = load_config(config)
    rules = {}
    for name, spec in (data.get("codemetrics", {}).get("rules", {}) or {}).items():
        if not isinstance(spec, list) or len(spec) < 2 or spec[0] == "off":
            continue
        options = spec[1] if isinstance(spec[1], dict) else {}
        rules[name] = {"severity": severity_for(str(spec[0])), "options": options}
    return rules


def load_exceptions(config: Path) -> list[dict]:
    """The `.dependably` grandfathered exceptions that are still in force.

    The tool itself only accepts exceptions for its six metric rules (a
    namespace-level `architectural-rigidity` diagnosis cannot be excepted);
    entries for unknown rules are therefore ignored here exactly as the tool
    ignores them. An expired entry no longer suppresses anything.
    """
    data = load_config(config)
    entries = (data.get("codemetrics", {}) or {}).get("exceptions", []) or []
    today = datetime.date.today()
    active = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("rule") not in RULE_METRICS:
            continue
        expires = entry.get("expires")
        if expires:
            try:
                if datetime.date.fromisoformat(str(expires)) < today:
                    continue
            except ValueError:
                continue
        active.append(entry)
    return active


def entry_name(section: str, entry: dict) -> str:
    if section == "Methods":
        return f"{entry.get('Type')}.{entry.get('Name')}"
    namespace = entry.get("Namespace") or ""
    return f"{namespace}.{entry.get('Name')}" if namespace else (entry.get("Name") or "?")


def synthesized_findings(data: dict, rules: dict[str, dict]) -> list[dict]:
    """Rule breaches codemetrics gates on but omits from its `findings` array."""
    metrics = data.get("extra", {}).get("metrics", {})
    out = []
    for rule, spec in rules.items():
        mapping = RULE_METRICS.get(rule)
        if mapping is None:
            continue
        section, field, direction = mapping
        key = "max" if direction == "max" else "min"
        limit = spec["options"].get(key)
        if limit is None:
            continue
        template, remediation = RULE_TEXT[rule]
        for entry in metrics.get(section) or []:
            value = entry.get(field)
            if value is None:
                continue
            breached = value > limit if direction == "max" else value < limit
            if not breached:
                continue
            out.append({
                "ruleId": rule,
                "severity": spec["severity"],
                "weight": abs(value - limit),
                "_symbol": entry_name(section, entry),
                "location": {"file": entry.get("File") or "(unknown)", "line": entry.get("StartLine") or 1},
                "message": template.format(value=value, limit=limit, name=entry_name(section, entry)),
                "remediation": remediation,
            })
    return out


def finding_key(finding: dict) -> tuple:
    location = finding.get("location") or {}
    return (finding.get("ruleId"), location.get("file"), location.get("line"))


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def symbols_match(entry: dict, symbol: str, file: str) -> bool:
    """Selector match for a `.dependably` exception: `path` (suffix, repo-relative)
    and `symbol` (fully-qualified type name or its simple name). Any selector may
    be omitted; a supplied selector must match."""
    path = entry.get("path")
    if path and not normalize_path(file).endswith(normalize_path(str(path))):
        return False
    selector = entry.get("symbol")
    if selector:
        selector = str(selector)
        if symbol != selector and not symbol.endswith("." + selector):
            return False
    return True


def stripped_config(config: Path, workdir: Path) -> Path:
    """The gate config minus `exceptions`, in a temp file (same directory every
    run so results stay reproducible). codemetrics' own JSON omits lcom4/
    coupling/nesting breaches, so only an unexcepted scan exposes them."""
    data = load_config(config)
    codemetrics = data.get("codemetrics")
    if isinstance(codemetrics, dict):
        codemetrics.pop("exceptions", None)
    path = workdir / (config.stem + ".no-exceptions.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def apply_exceptions(
    findings: list[dict], exceptions: list[dict], limits: dict[str, float]
) -> tuple[list[dict], list[dict]]:
    """Split findings into (open, accepted) using the repo's grandfathered
    exceptions. Each exception only absorbs a breach of the same rule whose
    excess over the limit is no larger than the excepted symbol's own excess
    (so an exception can't silently swallow a worse offender), and whose
    `path`/`symbol` selectors match the finding."""
    remaining = list(findings)
    accepted = []
    for entry in exceptions:
        rule = str(entry.get("rule") or "")
        if rule not in limits:
            continue
        symbol = str(entry.get("symbol") or "")
        # The excepted symbol's own excess, from the synthesized finding it
        # would have produced (weight = |value - limit|).
        excess = next(
            (
                finding.get("weight")
                for finding in findings
                if finding.get("ruleId") == rule and finding.get("_symbol") == symbol
            ),
            None,
        )
        match = next(
            (
                finding
                for finding in remaining
                if finding.get("ruleId") == rule
                and symbols_match(
                    entry,
                    str(finding.get("_symbol") or ""),
                    str((finding.get("location") or {}).get("file") or ""),
                )
                and (excess is None or finding.get("weight") is None or finding["weight"] <= excess)
            ),
            None,
        )
        if match is not None:
            remaining.remove(match)
            accepted.append(match)
    return remaining, accepted


def rule_limits(rules: dict[str, dict]) -> dict[str, float]:
    return {
        name: spec["options"]["max" if RULE_METRICS[name][2] == "max" else "min"]
        for name, spec in rules.items()
        if name in RULE_METRICS
        and spec["options"].get("max" if RULE_METRICS[name][2] == "max" else "min") is not None
    }


def load_fail_on(config: Path) -> str:
    data = load_config(config)
    spec = (data.get("codemetrics", {}) or {}).get("failOn") or {}
    severity = str(spec.get("severity") or "high")
    return severity if severity in SEVERITY_RANK else "high"


def gate_relevant(findings: list[dict], fail_on: str) -> list[dict]:
    """Only findings at or above the failOn severity gate (and therefore belong
    in the queue). A synthesized breach below it is accepted policy — the tool
    exits 0 for it — so listing it as an offender leaves the loop red with no
    actionable work."""
    threshold = SEVERITY_RANK[fail_on]
    return [
        finding
        for finding in findings
        if SEVERITY_RANK.get(str(finding.get("severity")), SEVERITY_RANK["high"]) <= threshold
    ]


def collect_findings(data: dict, rules: dict[str, dict]) -> list[dict]:
    """Tool findings plus synthesized rule breaches, deduplicated by rule+location.
    Synthesized entries carry a private `_symbol` for exception matching; it is
    stripped before the queue/report are written."""
    findings = list(data.get("findings") or [])
    seen = {finding_key(f) for f in findings}
    for finding in synthesized_findings(data, rules):
        if finding_key(finding) not in seen:
            findings.append(finding)
            seen.add(finding_key(finding))
    return findings


def severity_counts(findings: list[dict]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1
    return counts


def queue_md(data: dict) -> str:
    metrics = data["extra"]["metrics"]
    methods = metrics["Methods"]
    findings = data["findings"]
    accepted = data.get("accepted") or []
    s = metrics["Summary"]

    lines = [
        "# Metrics queue (worst offenders, worst-first)",
        "",
        f"Generated by `{TOOL}` (schema v{data['schemaVersion']}) at `{data['extra']['Meta']['ToolVersion']}`",
        f"Target: `{data['target']}` — {s['Files']} files, {s['Types']} types, {s['Methods']} methods, {s['TotalSloc']} SLOC",
        "",
        f"Summary: avg cyclomatic {s['AverageCyclomatic']}, avg cognitive {s['AverageCognitive']}, "
        f"avg maintainability index {s['AverageMaintainabilityIndex']} (max cc {s['MaxCyclomatic']}, max cognitive {s['MaxCognitive']})",
        "",
        f"Findings: {len(findings)} total "
        f"({data['summary']['bySeverity']['critical']} critical / {data['summary']['bySeverity']['high']} high / "
        f"{data['summary']['bySeverity']['moderate']} moderate / {data['summary']['bySeverity']['low']} low)",
        "",
        "## Findings (worst-first by severity)",
        "",
    ]
    severity_order = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
    for f in sorted(findings, key=lambda f: (severity_order.get(f["severity"], 9), -f.get("weight", 0))):
        loc = f["location"]
        file = loc.get("file") or "(namespace level)"
        line = loc.get("line") or ""
        lines.append(f"- **{f['severity']}** `{f['ruleId']}` [{file}:{line}] {f['message']}")
        lines.append(f"  - Fix: {f['remediation']}")
    if accepted:
        lines += ["", "## Accepted (grandfathered by `.dependably` exceptions)", ""]
        for f in accepted:
            loc = f["location"]
            file = loc.get("file") or "(namespace level)"
            line = loc.get("line") or ""
            lines.append(f"- `{f['ruleId']}` [{file}:{line}] {f['message']}")
    lines += ["", "## Lowest maintainability index (methods)", ""]
    for m in sorted(methods, key=lambda m: m["MaintainabilityIndex"])[:15]:
        lines.append(
            f"- MI {m['MaintainabilityIndex']:>5} | cc {m['Cyclomatic']:>2} | sloc {m['Sloc']:>4} | "
            f"{m['Type']}.{m['Name']} ({m['File']}:{m['StartLine']})"
        )
    lines += ["", "## Highest cyclomatic complexity (methods)", ""]
    for m in sorted(methods, key=lambda m: m["Cyclomatic"], reverse=True)[:10]:
        lines.append(
            f"- cc {m['Cyclomatic']:>2} | cognitive {m['Cognitive']:>2} | MI {m['MaintainabilityIndex']:>5} | "
            f"{m['Type']}.{m['Name']} ({m['File']}:{m['StartLine']})"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-gate", action="store_true", help="always exit 0")
    args = parser.parse_args()

    tool = find_tool(TOOL)
    if not tool:
        print(f"error: `{TOOL}` not found on PATH or ~/.dotnet/tools", file=sys.stderr)
        print("install: dotnet tool install --global Dependably.CodeMetrics", file=sys.stderr)
        return 2

    # `codemetrics` currently discovers 0 files when given a .sln (it globs C#
    # sources itself); pass the repo root and the gate config explicitly.
    config = gate_config()
    proc = subprocess.run(
        [tool, str(REPO), "--format", "json", "--config", str(config)],
        capture_output=True, text=True, cwd=REPO,
    )
    if proc.returncode not in (0, 1):
        print(proc.stderr, file=sys.stderr)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("error: codemetrics output was not valid JSON", file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        return 2

    REPORT.write_text(proc.stdout)
    rules = load_rules(config)
    exceptions = load_exceptions(config)
    raw = data
    if exceptions:
        # codemetrics suppresses excepted breaches from its JSON but does not
        # say which; an unexcepted scan recovers them so they can be matched
        # against the selectors and listed as accepted instead of as work.
        with tempfile.TemporaryDirectory(prefix="codemetrics-") as tmp:
            bare = stripped_config(config, Path(tmp))
            bare_proc = subprocess.run(
                [tool, str(REPO), "--format", "json", "--config", str(bare)],
                capture_output=True, text=True, cwd=REPO,
            )
        if bare_proc.returncode in (0, 1):
            try:
                raw = json.loads(bare_proc.stdout)
            except json.JSONDecodeError:
                raw = data
        else:
            print(bare_proc.stderr, file=sys.stderr)

    findings = collect_findings(raw, rules)
    open_findings, accepted = apply_exceptions(
        findings, exceptions, rule_limits(rules)
    )
    open_findings = gate_relevant(open_findings, load_fail_on(config))
    for finding in open_findings + accepted:
        finding.pop("_symbol", None)
    data["findings"] = open_findings
    data["accepted"] = accepted
    data["summary"] = {**data["summary"],
                       "findings": len(open_findings),
                       "bySeverity": severity_counts(open_findings)}
    QUEUE.write_text(queue_md(data))

    s = data["summary"]
    print(f"codemetrics: {s['scanned']} files scanned, {s['findings']} findings "
          f"(high+ {s['bySeverity']['high'] + s['bySeverity']['critical']}), "
          f"{len(accepted)} accepted, gate exit = {proc.returncode}")
    print(f"config: {config.name}")
    print(f"reports: {REPORT.name}, {QUEUE.name}")
    return 0 if args.no_gate else proc.returncode


if __name__ == "__main__":
    sys.exit(main())