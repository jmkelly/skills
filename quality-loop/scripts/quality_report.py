#!/usr/bin/env python3
"""Report mode for the quality loop: fresh audits -> implementor report + bd plan.

`quality-loop.py --report` runs every enabled audit once (a fresh run — stale
queue files are never read) and hands the results to this module, which writes
two artifacts to the repo root:

    quality-report.md    brief implementor report: per-gate status, the ticket
                         DAG (parallel waves), top offenders, worker rules
    quality-plan.json    `bd create --graph` plan (beads): one ticket per
                         (file, gate), same-file tickets chained, one final
                         verification ticket fanning in on all of them

Ticket scoping follows beads conventions (small, verifiable, explicit
blocked-by edges) and is built for parallel implementation (pstack-style):

  - one ticket = one worker = one source file + one gate; a ticket's offenders
    are capped at the loop's batch size, chunks of the same file are chained;
  - tickets on different files have no edge between them, so they are ready
    in parallel; tickets on the same file are chained because two workers
    editing one file will conflict at merge time;
  - one verification ticket depends on every work ticket, so it only becomes
    ready (and runnable) when the whole queue is closed.

Every ticket carries its offenders (file:line + metric), the fix approach,
the file-ownership rule, the anti-gaming rules, and a concrete acceptance
criterion (the exact audit command that proves it). The plan JSON is exactly
the schema `bd create --graph` consumes; import it with one command.

This module is a library driven by quality-loop.py (which owns running the
audits); everything it reads comes from the JSON artifacts of that fresh run.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CRAP_THRESHOLD = 10  # the loop always runs the CRAP audit at its default gate
# Filesystem timestamps are stamped from the kernel's coarse (tick) clock, so a
# file written immediately after time.time() can carry an mtime a tick earlier.
FRESHNESS_GRACE = 0.05  # seconds
EPIC_KEY = "epic"
VERIFY_KEY = "verify"

REPORT_NAME = "quality-report.md"
PLAN_NAME = "quality-plan.json"

STACK_DIR = {"dotnet": "dotnet", "python": "python"}

GATE_ORDER = {
    "dotnet": ("quality", "coverage", "metrics", "warnings", "stryker"),
    "python": ("quality", "metrics", "warnings"),
}

GATE_ARTIFACT = {
    "quality": "crap-report.json",
    "coverage": "coverage-report.json",
    "metrics": "metrics-report.json",
    "warnings": "warnings-report.json",
    "stryker": "stryker-report.json",
}

AUDIT_SCRIPT = {
    "quality": "audit.py",
    "coverage": "coverage-audit.py",
    "metrics": "metrics-audit.py",
    "warnings": "warnings-audit.py",
    "stryker": "stryker-audit.py",
}

GATE_LABEL = {
    "quality": "CRAP",
    "coverage": "Coverage",
    "metrics": "Metrics",
    "warnings": "Warnings",
    "stryker": "Mutation",
}

# Ticket titles count these nouns ("5 offenders", "1 untested method").
GATE_NOUN = {
    "quality": "offender",
    "coverage": "untested method",
    "metrics": "finding",
    "warnings": "finding",
    "stryker": "survivor",
}

# Beads issue types: refactors/tests are tasks, warning cleanup is a chore.
GATE_TYPE = {
    "quality": "task",
    "coverage": "task",
    "metrics": "task",
    "warnings": "chore",
    "stryker": "task",
}

GATE_FIX = {
    "quality": (
        "reduce complexity as the primary lever (extract methods/functions, guard clauses, "
        "table-driven dispatch); add REAL tests only when that is the cheap lever for a "
        "low-complexity untested member. Never ExcludeFromCodeCoverage or `# pragma: no cover`."
    ),
    "coverage": (
        "add REAL unit/integration tests, most uncovered lines first; mocks/smoke tests only "
        "for I/O-bound methods. Never ExcludeFromCodeCoverage or fake tests."
    ),
    "metrics": (
        "raise Maintainability Index / lower cyclomatic, cognitive, nesting and coupling "
        "(extract, split, clarify). Use .dependably exceptions only to grandfather a "
        "deliberate accept (reason + expiry), never to hide work."
    ),
    "warnings": (
        "fix the warning — remove the unused code or apply the analyzer's suggested API. A "
        "*specific* suppression entry with a documented reason is legitimate repo policy; "
        "blanket suppression is not."
    ),
    "stryker": (
        "surviving mutants are tests that don't detect faults — add or tighten the test that "
        "should kill each mutant (or refactor if the mutation exposes dead code)."
    ),
}

ANTI_GAMING = (
    "Anti-gaming (SKILL.md): no coverage suppression, no fake/no-op tests, no weakened "
    "behavior, no cohesionless line-shuffling — the gate must be earned honestly."
)


# --------------------------------------------------------------------- model

@dataclass(frozen=True)
class AuditRun:
    """One audit in the report run: fresh status plus when it started."""

    name: str
    status: str  # "passed" | "failed" | "skipped"
    started: float | None = None  # epoch seconds; None when never started
    detail: str = ""  # e.g. "exit 1", "prerequisite gate failed: quality"


@dataclass(frozen=True)
class Offender:
    text: str  # one markdown bullet for the ticket description
    severity: str = "moderate"  # "high" | "moderate"


@dataclass
class Ticket:
    key: str
    title: str
    type: str
    priority: int
    gate: str
    file: str
    offenders: list[Offender] = field(default_factory=list)
    chunks: int = 1
    chunk: int = 1
    depends_on: list[str] = field(default_factory=list)
    description: str = ""
    acceptance: str = ""
    metadata: dict = field(default_factory=dict)

    def is_chunked(self) -> bool:
        return self.chunks > 1


@dataclass
class TicketCollection:
    tickets: list[Ticket] = field(default_factory=list)
    offenders: dict[str, dict[str, list[Offender]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportResult:
    report_path: Path
    plan_path: Path | None
    tickets: list[Ticket]
    verify: Ticket | None
    failed: list[str]
    notes: list[str]
    waves: list[list[Ticket]] = field(default_factory=list)


# ------------------------------------------------------------------ readers

def load_json(path: Path) -> dict:
    """Parsed JSON object, or {} when missing/unreadable (reported by caller)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def rel_path(repo: Path, raw: str) -> str:
    """Repo-relative posix path; keeps pseudo-paths like '<solution>' verbatim."""
    if not raw or raw.startswith("<"):
        return raw or "(unknown)"
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def clip(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def group_offenders(entries: list[tuple[str, Offender]], sort_key=None) -> dict[str, list[Offender]]:
    grouped: dict[str, list[Offender]] = {}
    for file, offender in entries:
        grouped.setdefault(file, []).append(offender)
    if sort_key is not None:
        for file in grouped:
            grouped[file].sort(key=sort_key)
    return dict(sorted(grouped.items()))


def read_crap_offenders(repo: Path) -> tuple[dict[str, list[Offender]], list[str]]:
    """Failing methods from crap-report.json (both stacks share the schema)."""
    data = load_json(repo / "crap-report.json")
    entries: list[tuple[str, Offender, float]] = []
    for method in data.get("methods", []):
        crap = float(method.get("crap") or 0)
        if crap < CRAP_THRESHOLD:
            continue
        file = rel_path(repo, str(method.get("filePath") or ""))
        name = f"{method.get('className','')}.{method.get('methodName','?')}".lstrip(".")
        entries.append((file, Offender(
            text=(f"CRAP {method.get('crap')} · cx {method.get('complexity')} · "
                  f"cov {method.get('coverage')}% · `{file}:{method.get('lineNumber')}` `{name}`"),
            severity=str(method.get("severity") or "moderate"),
        ), crap))
    entries.sort(key=lambda e: -e[2])
    return group_offenders([(f, o) for f, o, _ in entries]), []


def read_coverage_offenders(repo: Path) -> tuple[dict[str, list[Offender]], list[str]]:
    """Untested authored methods from coverage-report.json (.NET)."""
    data = load_json(repo / "coverage-report.json")
    entries: list[tuple[str, Offender]] = []
    for method in data.get("queue", []):
        file = rel_path(repo, str(method.get("filePath") or ""))
        name = f"{method.get('className','')}.{method.get('methodName','?')}".lstrip(".")
        uncovered = int(method.get("uncoveredLines") or 0)
        complexity = int(method.get("complexity") or 0)
        # No severity field upstream: worst-uncovered, high-complexity methods
        # are the ones worth scheduling first.
        severity = "high" if uncovered >= 15 or complexity >= 15 else "moderate"
        entries.append((file, Offender(
            text=(f"cx {complexity} · {uncovered} uncovered lines · "
                  f"`{file}:{method.get('lineNumber')}` `{name}`"),
            severity=severity,
        )))
    return group_offenders(entries), []


def metric_severity(raw: str) -> str:
    return "high" if raw in ("critical", "high") else "moderate"


def read_metrics_offenders(repo: Path) -> tuple[dict[str, list[Offender]], list[str]]:
    """Gate-relevant findings from metrics-report.json (both stacks)."""
    data = load_json(repo / "metrics-report.json")
    entries: list[tuple[str, Offender]] = []
    notes: list[str] = []
    skipped_ns = 0
    for finding in data.get("findings", []):
        location = finding.get("location") or {}
        raw_file = location.get("file")
        if not raw_file:
            skipped_ns += 1
            continue
        file = rel_path(repo, str(raw_file))
        rule = finding.get("ruleId", "?")
        line = location.get("line")
        location_txt = f"`{file}:{line}`" if line else f"`{file}`"
        entries.append((file, Offender(
            text=(f"{rule} ({finding.get('severity', 'high')}) · {location_txt} — "
                  f"{clip(str(finding.get('message', '')))}"),
            severity=metric_severity(str(finding.get("severity", "high"))),
        )))
    if skipped_ns:
        notes.append(f"{skipped_ns} namespace-level finding(s) cannot be file-scoped (see metrics-queue.md)")
    return group_offenders(entries), notes


def strip_location_prefix(message: str, raw_file: str, line: int, column: int) -> str:
    """Drop pyflakes' own `path:line:col: ` prefix — the location is already
    rendered from the structured fields, so the absolute path would repeat.

    pyflakes embeds the column in the message but may report column 0 in its
    JSON, so match the prefix loosely (`path:line[:col]: `)."""
    for candidate in (f"{raw_file}:{line}:{column}: ", f"{raw_file}:{line}: "):
        if raw_file and message.startswith(candidate):
            return message[len(candidate):]
    if raw_file and message.startswith(f"{raw_file}:"):
        tail = message[len(raw_file):]
        match = re.match(r":\d+(?::\d*)?: ", tail)
        if match:
            return tail[match.end():]
    return message


def read_warnings_offenders(repo: Path) -> tuple[dict[str, list[Offender]], list[str]]:
    """Findings from warnings-report.json (both stacks share the schema shape)."""
    data = load_json(repo / "warnings-report.json")
    entries: list[tuple[str, Offender]] = []
    for warning in data.get("warnings", []):
        raw_file = str(warning.get("filePath") or "")
        file = rel_path(repo, raw_file)
        line = warning.get("lineNumber") or 0
        code = str(warning.get("code") or "warning")
        message = strip_location_prefix(str(warning.get("message", "")), raw_file, line,
                                        warning.get("column") or 0)
        location = f"`{file}:{line}`" if line else f"`{file}`"
        entries.append((file, Offender(
            text=f"{code} · {location} — {clip(message)}",
            severity="moderate",
        )))
    return group_offenders(entries), []


def read_stryker_offenders(repo: Path) -> tuple[dict[str, list[Offender]], list[str]]:
    """Surviving mutants from stryker-report.json (written by stryker-audit.py)."""
    data = load_json(repo / "stryker-report.json")
    entries: list[tuple[str, Offender, int]] = []
    notes: list[str] = []
    for project in data.get("projects", []):
        if project.get("passed") is False and not project.get("files"):
            notes.append(
                f"stryker: project {project.get('name', '?')} is below break but has no surviving "
                "mutants (mutants went uncovered — work the coverage/quality gates first)"
            )
        severity = "moderate" if project.get("passed", True) else "high"
        for raw_file, mutants in (project.get("files") or {}).items():
            file = rel_path(repo, str(raw_file))
            for mutant in mutants:
                line = mutant.get("line")
                entries.append((file, Offender(
                    text=(f"`{file}:{line}` `{mutant.get('mutator', '?')}` → "
                          f"{mutant.get('replacement', '')!r} ({project.get('name', '?')})"),
                    severity=severity,
                ), int(line or 0)))
    return group_offenders([(f, o) for f, o, _ in entries]), notes


READERS = {
    "quality": read_crap_offenders,
    "coverage": read_coverage_offenders,
    "metrics": read_metrics_offenders,
    "warnings": read_warnings_offenders,
    "stryker": read_stryker_offenders,
}


def artifact_is_fresh(path: Path, started: float | None) -> bool:
    """True when the artifact was (re)written by the audit run that just failed.

    An mtime older than the audit's start means the audit crashed before
    rewriting it — reading it would report the previous tree (stale metrics),
    so the caller must ignore it instead. The grace absorbs the coarse
    filesystem timestamp clock (a write right after `started` can be stamped a
    tick earlier than it)."""
    if not path.exists():
        return False
    if started is None:
        return True
    return path.stat().st_mtime >= started - FRESHNESS_GRACE


# ------------------------------------------------------------------ tickets

def chunked(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[i: i + size] for i in range(0, len(items), size)]


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def make_ticket(stack: str, gate: str, file: str, offenders: list[Offender], chunk_no: int,
                chunks: int, auds: dict, script_dir: Path, generated: str) -> Ticket:
    severity = "high" if any(o.severity == "high" for o in offenders) else "moderate"
    title = f"{GATE_LABEL[gate]}: {file}"
    if chunks > 1:
        title += f" [{chunk_no}/{chunks}]"
    title += f" — {plural(len(offenders), GATE_NOUN[gate])}"
    queue = auds[gate][1]
    metadata = {
        "gate": gate,
        "file": file,
        "offenders": len(offenders),
        "queue": queue,
        "report": REPORT_NAME,
    }
    if chunks > 1:
        metadata["chunk"] = f"{chunk_no}/{chunks}"
    ticket = Ticket(key="", title=title, type=GATE_TYPE[gate],
                    priority=1 if severity == "high" else 2,
                    gate=gate, file=file, offenders=offenders,
                    chunks=chunks, chunk=chunk_no, metadata=metadata)
    ticket.description = ticket_description(ticket, queue, generated)
    ticket.acceptance = acceptance_for(stack, gate, file, script_dir)
    return ticket


def ticket_description(ticket: Ticket, queue: str, generated: str) -> str:
    lines = [
        f"{GATE_LABEL[ticket.gate]} gate for `{ticket.file}`"
        + (f" (chunk {ticket.chunk}/{ticket.chunks})" if ticket.is_chunked() else "") + ".",
        "",
        f"Offenders from the fresh audit ({generated}):",
        "",
        *[f"- {o.text}" for o in ticket.offenders],
        "",
        f"Fix approach: {GATE_FIX[ticket.gate]}",
        "",
        f"File ownership: this ticket owns `{ticket.file}`. Same-file tickets are blocked by this "
        "one — never edit a file another open ticket owns; pull from `bd ready` so the DAG "
        "enforces it.",
        "",
        ANTI_GAMING,
        "",
        f"Source: `{REPORT_NAME}` · `{queue}`.",
    ]
    return "\n".join(lines)


def audit_command(stack: str, gate: str, script_dir: Path) -> str:
    return f"python3 {script_dir / STACK_DIR[stack] / AUDIT_SCRIPT[gate]}"


def acceptance_for(stack: str, gate: str, file: str, script_dir: Path) -> str:
    cmd = audit_command(stack, gate, script_dir)
    paste = "Paste the command's tail into this ticket before closing."
    if gate == "quality":
        return (f"`{cmd}` (from the repo root) exits 0 for the listed members — they no longer "
                f"appear in crap-queue.md — and the full test suite is green. {paste}")
    if gate == "coverage":
        return (f"`{cmd}` shows the listed methods covered (gone from coverage-queue.md) and "
                f"authored branch coverage at or above the policy floor. {paste}")
    if gate == "metrics":
        return (f"`{cmd}` reports no remaining findings for `{file}` at the failOn severity. {paste}")
    if gate == "warnings":
        return (f"`{cmd}` reports zero warnings for `{file}` (a specific, documented suppression "
                f"is the only accepted alternative). {paste}")
    return (f"`{cmd}` shows the listed mutants killed and every project at or above "
            f"thresholds.break. {paste} Tests stay green and behavior is unchanged.")


def make_verify_ticket(stack: str, script_dir: Path, dependencies: list[str]) -> Ticket:
    loop = script_dir / "quality-loop.py"
    gates = ", ".join(GATE_LABEL[g] for g in GATE_ORDER[stack])
    return Ticket(
        key=VERIFY_KEY,
        title="Verify: all quality gates green (fresh run)",
        type="task",
        priority=1,
        gate="verify",
        file="",
        depends_on=dependencies,
        description="\n".join([
            "Run every enabled audit fresh and confirm the whole DAG landed green.",
            "",
            "Command (from the repo root):",
            "",
            f"    python3 {loop} --report",
            "",
            f"Exit 0 and a regenerated `{REPORT_NAME}` with zero tickets means every gate "
            f"passed ({gates}).",
            "",
            "If a gate is red, the report lists the remaining offenders — file new tickets from "
            "it (or hand it to the loop's implementor) instead of closing this one.",
            "",
            "Evidence required: the command's tail and the regenerated gate table (proof of a "
            "fresh run, not of queue files someone last wrote days ago).",
        ]),
        acceptance=(f"`python3 {loop} --report` exits 0 and the regenerated `{REPORT_NAME}` "
                    "lists no tickets. Paste the command's tail as evidence."),
        metadata={"gate": "verify", "report": REPORT_NAME},
    )


def collect_tickets(stack: str, repo: Path, auds: dict, runs: list[AuditRun], batch: int,
                    script_dir: Path, generated: str) -> TicketCollection:
    """Fresh-run tickets: one per (file, gate), worst-first chunks, same-file chains."""
    runs_by_name = {run.name: run for run in runs}
    collection = TicketCollection()
    last_key_for_file: dict[str, str] = {}
    for gate in GATE_ORDER[stack]:
        run = runs_by_name.get(gate)
        if run is None or run.status != "failed":
            continue
        artifact = repo / GATE_ARTIFACT[gate]
        if not artifact.exists():
            collection.notes.append(f"{gate}: no {artifact.name} — audit failed without writing an artifact")
            continue
        if not artifact_is_fresh(artifact, run.started):
            collection.notes.append(
                f"{gate}: {artifact.name} predates this run — ignored (stale; not reported)")
            continue
        files, notes = READERS[gate](repo)
        collection.notes += [f"{gate}: {note}" for note in notes]
        collection.offenders[gate] = files
        for file in sorted(files):
            chunks = chunked(files[file], batch)
            for chunk_no, chunk in enumerate(chunks, 1):
                ticket = make_ticket(stack, gate, file, chunk, chunk_no, len(chunks),
                                     auds, script_dir, generated)
                ticket.key = f"t{len(collection.tickets) + 1}"
                previous = last_key_for_file.get(file)
                if previous:
                    ticket.depends_on.append(previous)
                collection.tickets.append(ticket)
                last_key_for_file[file] = ticket.key
    return collection


# --------------------------------------------------------------------- plan

def epic_node(title: str, description: str) -> dict:
    return {
        "key": EPIC_KEY,
        "title": title,
        "type": "epic",
        "priority": 2,
        "description": description,
        "labels": ["quality-loop"],
    }


def ticket_node(ticket: Ticket) -> dict:
    node = {
        "key": ticket.key,
        "title": ticket.title,
        "type": ticket.type,
        "priority": ticket.priority,
        "parent_key": EPIC_KEY,
        "description": ticket.description,
        "labels": ["quality-loop", f"gate:{ticket.gate}"],
    }
    if ticket.acceptance:
        node["acceptance_criteria"] = ticket.acceptance
    if ticket.metadata:
        node["metadata"] = ticket.metadata
    return node


def build_plan(epic_title: str, epic_description: str, tickets: list[Ticket],
               verify: Ticket, commit_message: str) -> dict:
    """bd `create --graph` plan: edges run dependent -> blocker (verified against bd)."""
    nodes = [epic_node(epic_title, epic_description)]
    nodes += [ticket_node(t) for t in tickets]
    nodes.append(ticket_node(verify))
    edges = [
        {"from_key": t.key, "to_key": dependency, "type": "blocks"}
        for t in tickets
        for dependency in t.depends_on
    ]
    # The verification ticket only becomes ready once every work ticket is closed.
    edges += [{"from_key": verify.key, "to_key": t.key, "type": "blocks"} for t in tickets]
    return {"commit_message": commit_message, "nodes": nodes, "edges": edges}


def compute_waves(tickets: list[Ticket]) -> list[list[Ticket]]:
    """Topological levels: wave 1 is ready now, wave N needs N-1 done first."""
    by_key = {t.key: t for t in tickets}
    level: dict[str, int] = {}

    def depth(key: str) -> int:
        if key in level:
            return level[key]
        dependencies = [d for d in by_key[key].depends_on if d in by_key]
        level[key] = 1 if not dependencies else 1 + max(depth(d) for d in dependencies)
        return level[key]

    for ticket in tickets:
        depth(ticket.key)
    waves: dict[int, list[Ticket]] = {}
    for ticket in tickets:
        waves.setdefault(level[ticket.key], []).append(ticket)
    return [waves[i] for i in sorted(waves)]


# ------------------------------------------------------------------- report

def git_sha(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, text=True,
            stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def gate_summary(gate: str, files: dict[str, list[Offender]]) -> str:
    items = [o for offenders in files.values() for o in offenders]
    high = sum(1 for o in items if o.severity == "high")
    if gate == "quality":
        return f"{plural(len(items), 'method')} with CRAP >= {CRAP_THRESHOLD}"
    if gate == "coverage":
        return f"{plural(len(items), 'untested method')} queued"
    if gate == "stryker":
        return f"{plural(len(items), 'surviving mutant')}"
    count = f"{plural(len(items), 'finding')}"
    return f"{count} ({high} high)" if high else count


def status_cell(run: AuditRun, offenders: dict[str, list[Offender]]) -> str:
    if run.status == "passed":
        return "PASS"
    if run.status == "skipped":
        return f"NOT RUN ({run.detail})"
    if offenders:
        return "FAIL"
    return f"FAIL ({run.detail or 'no fresh artifact'})"


def gate_table(enabled: list[str], auds: dict, runs: list[AuditRun],
               offenders: dict[str, dict[str, list[Offender]]]) -> list[str]:
    runs_by_name = {run.name: run for run in runs}
    lines = ["| Gate | Status | Findings | Queue |", "|---|---|---|---|"]
    for gate in enabled:
        run = runs_by_name.get(gate)
        if run is None:
            continue
        files = offenders.get(gate, {})
        if run.status == "passed":
            findings = "—"
        elif run.status == "skipped":
            findings = "stale — not used"
        else:
            findings = gate_summary(gate, files) if files else "—"
        lines.append(f"| {gate} | {status_cell(run, files)} | {findings} | `{auds[gate][1]}` |")
    return lines


def wave_lines(waves: list[list[Ticket]]) -> list[str]:
    lines: list[str] = []
    for number, wave in enumerate(waves, 1):
        label = "ready now" if number == 1 else f"needs wave {number - 1} done"
        lines.append(f"### Wave {number} — {label}")
        lines.append("")
        for ticket in wave:
            blocked = ", ".join(d.upper() for d in ticket.depends_on) if ticket.depends_on else "—"
            chunk = f" [{ticket.chunk}/{ticket.chunks}]" if ticket.is_chunked() else ""
            gate = GATE_LABEL.get(ticket.gate) or ticket.gate.title()
            location = f" · `{ticket.file}`" if ticket.file else ""
            lines.append(f"- **{ticket.key.upper()}** [P{ticket.priority}] {gate}{location}{chunk} "
                         f"— blocked by: {blocked}")
        lines.append("")
    return lines


def top_offender_lines(gate: str, files: dict[str, list[Offender]], limit: int = 5) -> list[str]:
    items = [(file, o) for file, offenders in files.items() for o in offenders]
    if not items:
        return []
    lines = [f"**{GATE_LABEL[gate]}** — worst {min(limit, len(items))} of {len(items)}:"]
    lines += [f"- {o.text}" for _, o in items[:limit]]
    if len(items) > limit:
        lines.append(f"- … full list in `{GATE_ARTIFACT[gate]}` (see queue)")
    lines.append("")
    return lines


def render_report(stack: str, repo: Path, auds: dict, enabled: list[str], runs: list[AuditRun],
                  collection: TicketCollection, verify: Ticket | None,
                  waves: list[list[Ticket]], generated: str) -> str:
    runs_by_name = {run.name: run for run in runs}
    sha = git_sha(repo)
    failed = [run.name for run in runs if run.status == "failed"]
    not_run = [run.name for run in runs if run.status == "skipped"]

    header = [
        f"# Quality report — {repo.name} ({stack})",
        "",
        f"Generated {generated} by `quality-loop.py --report` — every enabled audit ran fresh; "
        "stale queue/report files were not read.",
        f"Commit: `{sha or 'n/a'}` · Gates: "
        + " · ".join(f"{name} {runs_by_name[name].status.upper()}" for name in enabled
                      if name in runs_by_name),
        "",
    ]

    body = ["## Gates", ""]
    body += gate_table(enabled, auds, runs, collection.offenders)
    body.append("")
    if not_run:
        body.append(f"Not run (prerequisites failed): {', '.join(not_run)} — their queues are stale and "
                    "were not used for tickets.")
        body.append("")

    if not collection.tickets and not failed:
        body += [
            "## Nothing to do",
            "",
            "All enabled gates are green. No tickets were generated.",
            "",
        ]
        if collection.notes:
            body += ["## Notes", ""] + [f"- {note}" for note in collection.notes] + [""]
        body.append("Keep the tree as-is; re-run `quality-loop.py --report` after new work.")
        return "\n".join(header + body) + "\n"

    widest = max((len(w) for w in waves), default=0)
    if collection.tickets:
        all_tickets = collection.tickets + ([verify] if verify else [])
        edge_count = sum(len(t.depends_on) for t in collection.tickets) + len(collection.tickets)
        body += [
            f"## Ticket DAG — {plural(len(collection.tickets), 'ticket')} + verification",
            "",
            f"Import with `bd create --graph {PLAN_NAME}` (plan: {len(all_tickets)} nodes, "
            f"{edge_count} edges).",
            f"Parallelism: wave 1 has {len(waves[0]) if waves else 0} ready tickets; widest wave "
            f"{widest}; critical path {len(waves)} tickets (including the final verification).",
            "",
        ]
        body += wave_lines(waves)
    else:
        body += [
            "## No tickets generated",
            "",
            "Gate(s) failed but no fresh offender artifact was readable — see the notes below and "
            "the queue files; fix the audit/tooling before trusting the queues.",
            "",
        ]

    if collection.notes:
        body += ["## Notes", ""] + [f"- {note}" for note in collection.notes] + [""]

    body += ["## Top offenders (fresh)", ""]
    for gate in GATE_ORDER[stack]:
        if gate in collection.offenders:
            body += top_offender_lines(gate, collection.offenders[gate])
    if not collection.offenders:
        body += ["No fresh offenders could be extracted — see Notes above.", ""]
    if not collection.tickets:
        return "\n".join(header + body) + "\n"

    body += [
        "## Working the plan (implementor brief)",
        "",
        f"- Import: `bd create --graph {PLAN_NAME}` then `bd ready --exclude-type epic` for claimable work.",
        "- One ticket = one worker; claim with `bd update <id> --claim`.",
        "- Edit only the files your ticket owns (metadata `file`); same-file tickets are chained, so "
        "`bd ready` never hands two workers the same file.",
        "- Keep the build and the full test suite green; the acceptance criterion on each ticket names "
        "the exact audit command that must pass.",
        "- Paste the acceptance evidence (audit output tail) into the ticket before `bd close`.",
        f"- Re-running `--report` overwrites `{PLAN_NAME}` and `{REPORT_NAME}`; importing the plan "
        "twice duplicates the set — remove an old one with `bd delete <epic-id> --cascade --force`.",
        "- The verification ticket re-runs `--report` once every work ticket is closed; a red gate "
        "regenerates this report — file tickets from it.",
        "",
        "## Anti-gaming rules (the contract)",
        "",
        "- No `[ExcludeFromCodeCoverage]`, `#pragma warning disable`, `# pragma: no cover`, or fake/no-op tests.",
        "- No blanket `<NoWarn>` suppression; a specific code with a documented reason is repo policy.",
        "- Behavior must be preserved: no swallowed exceptions, removed validation, or weakened checks.",
        "- Split only into cohesive units — mechanical line-shuffling is rejected.",
        "",
    ]
    return "\n".join(header + body) + "\n"


# ------------------------------------------------------------------ driver

def write_report_and_plan(repo: Path, stack: str, auds: dict, enabled: list[str],
                          runs: list[AuditRun], batch: int, script_dir: Path,
                          now: datetime | None = None) -> ReportResult:
    """Collect fresh offenders, write quality-report.md and quality-plan.json."""
    generated = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    collection = collect_tickets(stack, repo, auds, runs, batch, script_dir, generated)
    verify = make_verify_ticket(stack, script_dir, [t.key for t in collection.tickets]) \
        if collection.tickets else None
    if not collection.tickets:
        # A plan from a previous, redder run must not linger as an importable
        # artifact now that every gate is green.
        stale_plan = repo / PLAN_NAME
        if stale_plan.exists():
            stale_plan.unlink()
            collection.notes.append(f"removed stale {PLAN_NAME} (no work to ticket)")
    waves = compute_waves(collection.tickets + ([verify] if verify else []))
    report_text = render_report(stack, repo, auds, enabled, runs, collection, verify, waves, generated)
    report_path = repo / REPORT_NAME
    report_path.write_text(report_text, encoding="utf-8")

    plan_path = None
    if collection.tickets and verify is not None:
        epic_title = f"Quality report — {repo.name} — {generated[:10]}"
        epic_description = "\n".join([
            f"DAG implementation queue from `quality-loop.py --report` ({generated}).",
            "",
            f"Source: `{REPORT_NAME}` · {plural(len(collection.tickets), 'work ticket')} + 1 verification.",
            f"Import: `bd create --graph {PLAN_NAME}` (once — re-importing duplicates the set).",
            "Work `bd ready --exclude-type epic` tickets; never start a blocked one.",
        ])
        plan = build_plan(epic_title, epic_description, collection.tickets, verify,
                          commit_message=f"quality report: {plural(len(collection.tickets), 'ticket')} + verification")
        plan_path = repo / PLAN_NAME
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    failed = [run.name for run in runs if run.status == "failed"]
    return ReportResult(report_path=report_path, plan_path=plan_path,
                        tickets=collection.tickets, verify=verify, failed=failed,
                        notes=collection.notes, waves=waves)
