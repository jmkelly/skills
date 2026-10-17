"""Tests for scripts/quality_report.py — the `--report` mode's ticket builder.

Covers the fresh-artifact readers (missing/stale artifacts never become
tickets), ticket scoping (file x gate, batch-size chunking, same-file chains,
priority from severity), the bd `create --graph` plan shape (epic parent, edge
direction, verification fan-in), wave computation, and the report/plan writer.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from tests.conftest import QUALITY_LOOP as ql
from tests.conftest import QUALITY_REPORT as qr


SCRIPTS = Path("/skill/scripts")
GENERATED = "2026-10-17T12:00:00+13:00"
NOW = datetime.fromisoformat(GENERATED)


def write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def crap_method(file: str, line: int, crap: float, complexity: int = 5,
                coverage: float = 0.0, severity: str = "moderate", name: str = "m") -> dict:
    return {"methodName": name, "className": "", "namespace": "", "filePath": file,
            "lineNumber": line, "crap": crap, "complexity": complexity,
            "coverage": coverage, "severity": severity}


def run(name: str, status: str, started: float | None = None, detail: str = "") -> qr.AuditRun:
    return qr.AuditRun(name, status, started, detail)


def python_auds() -> dict:
    return ql.build_audits("python")


def collect(repo: Path, runs: list[qr.AuditRun], batch: int = 5, stack: str = "python"):
    return qr.collect_tickets(stack, repo, ql.build_audits(stack), runs, batch, SCRIPTS, GENERATED)


def write_report_and_plan(repo: Path, runs: list[qr.AuditRun], batch: int = 5, stack: str = "python"):
    return qr.write_report_and_plan(repo, stack, ql.build_audits(stack),
                                    list(ql.build_audits(stack)), runs, batch, SCRIPTS, now=NOW)


# --------------------------------------------------------------------- readers

def test_rel_path_relative_and_absolute(tmp_path):
    assert qr.rel_path(tmp_path, "src/foo.py") == "src/foo.py"
    assert qr.rel_path(tmp_path, str(tmp_path / "src" / "foo.py")) == "src/foo.py"
    assert qr.rel_path(tmp_path, "<solution>") == "<solution>"
    assert qr.rel_path(tmp_path, "") == "(unknown)"
    assert qr.rel_path(tmp_path, "/outside/repo.rs") == "/outside/repo.rs"


def test_load_json_missing_and_invalid(tmp_path):
    assert qr.load_json(tmp_path / "nope.json") == {}
    (tmp_path / "bad.json").write_text("{not json")
    assert qr.load_json(tmp_path / "bad.json") == {}
    (tmp_path / "list.json").write_text("[1, 2]")
    assert qr.load_json(tmp_path / "list.json") == {}


def test_artifact_freshness_grace_and_staleness(tmp_path):
    artifact = write_json(tmp_path / "crap-report.json", {"methods": []})
    mtime = artifact.stat().st_mtime
    # a write in the same coarse timestamp tick as the audit start is fresh
    assert qr.artifact_is_fresh(artifact, mtime + 0.01)
    # an artifact older than the run (a real previous run) is stale
    assert not qr.artifact_is_fresh(artifact, mtime + 1.0)
    assert not qr.artifact_is_fresh(tmp_path / "missing.json", time.time())


def test_read_crap_filters_threshold_sorts_and_formats(tmp_path):
    write_json(tmp_path / "crap-report.json", {"methods": [
        crap_method("src/foo.py", 10, 12.5, complexity=4, coverage=25.0, name="bark"),
        crap_method("src/foo.py", 40, 42.0, complexity=9, coverage=0.0, severity="high", name="meow"),
        crap_method("src/foo.py", 99, 3.0, complexity=1, name="fine"),
        crap_method(str(tmp_path / "src" / "bar.py"), 5, 15.0, name="woof"),
    ]})
    files, notes = qr.read_crap_offenders(tmp_path)
    assert notes == []
    assert list(files) == ["src/bar.py", "src/foo.py"]
    assert [o.text.split(" · ")[0] for o in files["src/foo.py"]] == ["CRAP 42.0", "CRAP 12.5"]
    assert "cov 0.0%" in files["src/foo.py"][0].text
    assert "`src/foo.py:40` `meow`" in files["src/foo.py"][0].text
    assert files["src/foo.py"][0].severity == "high"
    assert files["src/bar.py"][0].severity == "moderate"


def test_read_crap_missing_report(tmp_path):
    files, notes = qr.read_crap_offenders(tmp_path)
    assert files == {} and notes == []


def test_read_coverage_severity_heuristic(tmp_path):
    write_json(tmp_path / "coverage-report.json", {"queue": [
        {"className": "Svc", "methodName": "Big", "filePath": "A.cs", "lineNumber": 1,
         "complexity": 3, "uncoveredLines": 20},
        {"className": "Svc", "methodName": "Small", "filePath": "A.cs", "lineNumber": 9,
         "complexity": 2, "uncoveredLines": 3},
    ]})
    files, _ = qr.read_coverage_offenders(tmp_path)
    assert [o.severity for o in files["A.cs"]] == ["high", "moderate"]
    assert "20 uncovered lines" in files["A.cs"][0].text
    assert "`A.cs:1` `Svc.Big`" in files["A.cs"][0].text


def test_read_metrics_skips_namespace_findings_and_maps_severity(tmp_path):
    write_json(tmp_path / "metrics-report.json", {"findings": [
        {"ruleId": "cyclomatic", "severity": "critical", "message": "cc 40",
         "location": {"file": "src/a.py", "line": 3}},
        {"ruleId": "mi", "severity": "moderate", "message": "mi low",
         "location": {"file": "src/a.py", "line": 1}},
        {"ruleId": "coupling", "severity": "high", "message": "namespace-wide",
         "location": {"file": None, "line": None}},
    ]})
    files, notes = qr.read_metrics_offenders(tmp_path)
    assert [o.severity for o in files["src/a.py"]] == ["high", "moderate"]
    assert "cyclomatic (critical)" in files["src/a.py"][0].text
    assert len(notes) == 1 and "namespace-level" in notes[0]


def test_read_warnings_keeps_pseudo_paths_and_clips(tmp_path):
    write_json(tmp_path / "warnings-report.json", {"warnings": [
        {"code": "MSB3277", "filePath": "<solution>", "lineNumber": 0, "message": "x" * 300},
        {"code": "CS0219", "filePath": str(tmp_path / "src" / "a.cs"), "lineNumber": 4,
         "message": "unused"},
        {"code": "warning", "filePath": str(tmp_path / "src" / "b.py"), "lineNumber": 2,
         "column": 0, "message": f"{tmp_path}/src/b.py:2:1: 'os' imported but unused"},
    ]})
    files, _ = qr.read_warnings_offenders(tmp_path)
    assert list(files) == ["<solution>", "src/a.cs", "src/b.py"]
    assert "MSB3277 · `<solution>`" in files["<solution>"][0].text
    assert files["<solution>"][0].text.endswith("…")
    assert "CS0219 · `src/a.cs:4` — unused" in files["src/a.cs"][0].text
    # pyflakes' own path prefix is dropped (the location is rendered already)
    assert files["src/b.py"][0].text == "warning · `src/b.py:2` — 'os' imported but unused"


def test_read_stryker_groups_survivors_and_notes_uncovered(tmp_path):
    write_json(tmp_path / "stryker-report.json", {
        "projects": [
            {"name": "A.Tests", "passed": False, "files": {"src/a.cs": [
                {"line": 7, "mutator": "String", "replacement": "''"},
                {"line": 2, "mutator": "Arithmetic", "replacement": "+"},
            ]}},
            {"name": "B.Tests", "passed": False, "files": {}},
        ]})
    files, notes = qr.read_stryker_offenders(tmp_path)
    assert [o.severity for o in files["src/a.cs"]] == ["high", "high"]
    assert "`src/a.cs:7` `String` → \"''\" (A.Tests)" in files["src/a.cs"][0].text
    assert len(notes) == 1 and "B.Tests" in notes[0] and "uncovered" in notes[0]


# --------------------------------------------------------------------- tickets

def test_collect_groups_per_file_and_gate_with_chains(tmp_path):
    started = time.time()  # before the artifacts, like the audit run itself
    write_json(tmp_path / "crap-report.json", {"methods": [
        crap_method("bar.py", 1, 42.0, severity="high", name="b"),
        crap_method("foo.py", 1, 12.0, name="f"),
    ]})
    write_json(tmp_path / "warnings-report.json", {"warnings": [
        {"code": "F401", "filePath": str(tmp_path / "bar.py"), "lineNumber": 2, "message": "unused"},
    ]})
    collection = collect(tmp_path, [run("quality", "failed", started),
                                    run("metrics", "passed"),
                                    run("warnings", "failed", started)])
    tickets = collection.tickets
    assert [t.key for t in tickets] == ["t1", "t2", "t3"]
    assert [(t.gate, t.file) for t in tickets] == [
        ("quality", "bar.py"), ("quality", "foo.py"), ("warnings", "bar.py")]
    assert tickets[0].priority == 1 and tickets[1].priority == 2 and tickets[2].priority == 2
    assert tickets[2].depends_on == ["t1"]  # same-file chain, across gates
    assert tickets[2].type == "chore"
    assert tickets[0].type == "task"
    assert tickets[0].title == "CRAP: bar.py — 1 offender"
    assert tickets[2].title == "Warnings: bar.py — 1 finding"
    assert tickets[0].metadata["gate"] == "quality"
    assert tickets[0].metadata["file"] == "bar.py"
    assert "File ownership" in tickets[0].description
    assert "bar.py:1" in tickets[0].description


def test_collect_chunks_offenders_and_chains(tmp_path):
    started = time.time()  # before the artifact, like the audit run itself
    write_json(tmp_path / "crap-report.json", {"methods": [
        crap_method("foo.py", i, 100 - i, name=f"m{i}") for i in range(5)
    ]})
    collection = collect(tmp_path, [run("quality", "failed", started)], batch=2)
    tickets = collection.tickets
    assert [t.key for t in tickets] == ["t1", "t2", "t3"]
    assert [(t.chunk, t.chunks) for t in tickets] == [(1, 3), (2, 3), (3, 3)]
    assert tickets[1].depends_on == ["t1"] and tickets[2].depends_on == ["t2"]
    assert tickets[0].title == "CRAP: foo.py [1/3] — 2 offenders"
    assert tickets[2].title == "CRAP: foo.py [3/3] — 1 offender"
    assert tickets[0].metadata["chunk"] == "1/3"
    assert "chunk" not in tickets[2].metadata or tickets[2].metadata["chunk"] == "3/3"


def test_collect_stale_artifact_is_ignored(tmp_path):
    artifact = write_json(tmp_path / "crap-report.json", {"methods": [crap_method("foo.py", 1, 42.0)]})
    old = time.time() - 3600
    os.utime(artifact, (old, old))
    collection = collect(tmp_path, [run("quality", "failed", time.time())])
    assert collection.tickets == []
    assert any("predates this run" in note for note in collection.notes)


def test_collect_missing_artifact_is_noted(tmp_path):
    collection = collect(tmp_path, [run("quality", "failed", time.time())])
    assert collection.tickets == []
    assert any("no crap-report.json" in note for note in collection.notes)


def test_collect_skipped_gate_never_yields_tickets(tmp_path):
    """A skipped gate's (fresh!) artifact must not become tickets — the loop
    skipped it precisely because its queue reflects a different tree."""
    write_json(tmp_path / "crap-report.json", {"methods": [crap_method("foo.py", 1, 42.0)]})
    collection = collect(tmp_path, [run("quality", "passed"), run("metrics", "passed"),
                                    run("warnings", "passed")])
    assert collection.tickets == [] and collection.notes == []


def test_collect_dotnet_skipped_stryker_ignored(tmp_path):
    write_json(tmp_path / "stryker-report.json", {"projects": [
        {"name": "A.Tests", "passed": False, "files": {"src/a.cs": [
            {"line": 1, "mutator": "M", "replacement": "x"}]}},
    ]})
    collection = collect(tmp_path, [
        run("quality", "passed"), run("coverage", "passed"), run("metrics", "passed"),
        run("warnings", "passed"), run("stryker", "skipped", None, "prerequisite gate failed: quality"),
    ], stack="dotnet")
    assert collection.tickets == []


# ------------------------------------------------------------------------ plan

def tickets_for_waves() -> list[qr.Ticket]:
    return [
        qr.Ticket(key="t1", title="a", type="task", priority=1, gate="quality", file="a.py"),
        qr.Ticket(key="t2", title="b", type="task", priority=1, gate="quality", file="a.py",
                  depends_on=["t1"]),
        qr.Ticket(key="t3", title="c", type="task", priority=2, gate="metrics", file="b.py"),
    ]


def test_compute_waves_levels():
    verify = qr.Ticket(key="verify", title="v", type="task", priority=1, gate="verify",
                       file="", depends_on=["t1", "t2", "t3"])
    waves = qr.compute_waves([*tickets_for_waves(), verify])
    assert [[t.key for t in wave] for wave in waves] == [["t1", "t3"], ["t2"], ["verify"]]


def test_compute_waves_ignores_unknown_dependency():
    ticket = qr.Ticket(key="t1", title="a", type="task", priority=1, gate="quality", file="a.py",
                       depends_on=["ghost"])
    assert [len(w) for w in qr.compute_waves([ticket])] == [1]


def test_build_plan_shape(tmp_path):
    work = tickets_for_waves()
    verify = qr.Ticket(key="verify", title="Verify", type="task", priority=1, gate="verify",
                       file="", depends_on=["t1", "t2", "t3"], acceptance="run it")
    plan = qr.build_plan("Epic", "desc", work, verify, "msg")
    assert plan["commit_message"] == "msg"
    nodes = plan["nodes"]
    assert [n["key"] for n in nodes] == ["epic", "t1", "t2", "t3", "verify"]
    assert nodes[0]["type"] == "epic" and "parent_key" not in nodes[0]
    assert all(n["parent_key"] == "epic" for n in nodes[1:])
    assert nodes[-1]["acceptance_criteria"] == "run it"
    assert nodes[1]["labels"] == ["quality-loop", "gate:quality"]
    # edges run dependent -> blocker (the direction bd consumes)
    assert {"from_key": "t2", "to_key": "t1", "type": "blocks"} in plan["edges"]
    for key in ("t1", "t2", "t3"):
        assert {"from_key": "verify", "to_key": key, "type": "blocks"} in plan["edges"]
    assert len(plan["edges"]) == 1 + 3
    assert json.loads(json.dumps(plan)) == plan  # bd reads this as JSON


def test_make_verify_ticket_depends_on_work(tmp_path):
    verify = qr.make_verify_ticket("python", SCRIPTS, ["t1", "t2"])
    assert verify.key == "verify" and verify.depends_on == ["t1", "t2"]
    assert str(SCRIPTS / "quality-loop.py") in verify.description
    assert "--report" in verify.acceptance


def test_audit_command_paths(tmp_path):
    assert qr.audit_command("python", "quality", SCRIPTS).endswith("python/audit.py")
    assert qr.audit_command("dotnet", "stryker", SCRIPTS).endswith("dotnet/stryker-audit.py")


# -------------------------------------------------------------------- report

def red_repo(tmp_path: Path) -> None:
    write_json(tmp_path / "crap-report.json", {"methods": [
        crap_method("bar.py", 1, 42.0, severity="high", name="b"),
        crap_method("foo.py", 1, 12.0, name="f"),
    ]})
    write_json(tmp_path / "warnings-report.json", {"warnings": [
        {"code": "F401", "filePath": str(tmp_path / "bar.py"), "lineNumber": 2, "message": "unused"},
    ]})


def test_write_report_and_plan_red(tmp_path):
    started = time.time()  # before the artifacts, like the audit run itself
    red_repo(tmp_path)
    result = write_report_and_plan(tmp_path, [
        run("quality", "failed", started), run("metrics", "passed"), run("warnings", "failed", started)])
    assert result.failed == ["quality", "warnings"]
    assert result.plan_path == tmp_path / "quality-plan.json"
    assert len(result.tickets) == 3
    assert [[t.key for t in w] for w in result.waves] == [["t1", "t2"], ["t3"], ["verify"]]

    report = (tmp_path / "quality-report.md").read_text()
    assert "| quality | FAIL | 2 methods with CRAP >= 10 | `crap-queue.md` |" in report
    assert "| metrics | PASS | — | `metrics-queue.md` |" in report
    assert "| warnings | FAIL | 1 finding | `warnings-queue.md` |" in report
    assert "### Wave 1 — ready now" in report
    assert "bd create --graph quality-plan.json" in report
    assert "File ownership" not in report  # ownership lives in the tickets
    assert "## Top offenders (fresh)" in report
    assert "CRAP 42.0" in report

    plan = json.loads((tmp_path / "quality-plan.json").read_text())
    assert [n["key"] for n in plan["nodes"]] == ["epic", "t1", "t2", "t3", "verify"]
    verify_edges = [e for e in plan["edges"] if e["from_key"] == "verify"]
    assert {e["to_key"] for e in verify_edges} == {"t1", "t2", "t3"}


def test_write_report_and_plan_clean_removes_stale_plan(tmp_path):
    stale = tmp_path / "quality-plan.json"
    stale.write_text("{}")
    result = write_report_and_plan(tmp_path, [
        run("quality", "passed"), run("metrics", "passed"), run("warnings", "passed")])
    assert result.failed == [] and result.plan_path is None and result.tickets == []
    assert not stale.exists()
    report = (tmp_path / "quality-report.md").read_text()
    assert "Nothing to do" in report and "No tickets were generated" in report
    assert any("removed stale" in note for note in result.notes)


def test_write_report_and_plan_missing_artifact_notes(tmp_path):
    result = write_report_and_plan(tmp_path, [run("quality", "failed", time.time(), "exit 1")])
    assert result.failed == ["quality"] and result.plan_path is None
    report = (tmp_path / "quality-report.md").read_text()
    assert "No tickets generated" in report
    assert "no crap-report.json" in report
    assert "FAIL (exit 1)" in report


def test_write_report_and_plan_skipped_gate_status_and_notes(tmp_path):
    started = time.time()  # before the artifacts, like the audit run itself
    red_repo(tmp_path)
    result = write_report_and_plan(tmp_path, [
        run("quality", "failed", started),
        run("metrics", "skipped", None, "prerequisite gate failed: quality"),
        run("warnings", "passed")])
    report = (tmp_path / "quality-report.md").read_text()
    assert "NOT RUN (prerequisite gate failed: quality)" in report
    assert "Not run (prerequisites failed): metrics" in report
    assert result.plan_path is not None


def test_gate_summary_counts(tmp_path):
    assert qr.gate_summary("quality", {}) == "0 methods with CRAP >= 10"
    assert qr.gate_summary("warnings", {"a.py": [qr.Offender("x", "moderate"),
                                                 qr.Offender("y", "high")]}) == "2 findings (1 high)"
    assert qr.gate_summary("stryker", {"a.cs": [qr.Offender("x")]}) == "1 surviving mutant"


def test_status_cell_variants():
    files = {"a.py": [qr.Offender("x")]}
    assert qr.status_cell(run("quality", "passed"), {}) == "PASS"
    assert qr.status_cell(run("quality", "failed", 1.0, "exit 1"), files) == "FAIL"
    assert qr.status_cell(run("quality", "failed", 1.0, "exit 1"), {}) == "FAIL (exit 1)"
    assert qr.status_cell(run("stryker", "skipped", None, "prerequisite gate failed: quality"), {}) == \
        "NOT RUN (prerequisite gate failed: quality)"


def test_top_offender_lines_caps():
    files = {"a.py": [qr.Offender(f"o{i}") for i in range(8)]}
    lines = qr.top_offender_lines("quality", files, limit=5)
    assert "worst 5 of 8" in lines[0]
    assert len([line for line in lines if line.startswith("- o")]) == 5
    assert any("full list" in line for line in lines)


def test_git_sha_outside_repo(tmp_path):
    assert qr.git_sha(tmp_path) == ""
