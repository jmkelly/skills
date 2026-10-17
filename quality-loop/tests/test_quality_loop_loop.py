"""Tests for scripts/quality-loop.py — the multi-iteration loop mechanics.

Covers the per-iteration audit runner (prerequisite blocking, outcome
reporting), pass/exhaustion detection and exit codes, config assembly from
quality-loop env vars, CLI arg parsing, and main().
"""
from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.conftest import QUALITY_LOOP as ql
from tests.conftest import QUALITY_REPORT as qr
from tests.conftest import fake_proc


def make_config(tmp_path: Path, **kw) -> ql.ImplementorConfig:
    defaults = dict(stack="python", batch=2, session_dir=tmp_path / "sessions",
                    model="", approve=True, auds=ql.build_audits("python"))
    defaults.update(kw)
    return ql.ImplementorConfig(**defaults)


# ----------------------------------------------------------- loop mechanics

def test_blocking_prereqs():
    assert ql.blocking_prereqs(("quality", "metrics"), ["metrics"]) == ["metrics"]
    assert ql.blocking_prereqs(("quality",), ["metrics"]) == []


def test_print_outcome_fail_appends():
    failed = []
    ql.print_outcome("quality", "crap-queue.md", "CRAP < 10", failed, passed=False)
    assert failed == ["quality"]


def test_print_outcome_pass(capsys):
    failed = ["quality"]
    ql.print_outcome("quality", "crap-queue.md", "CRAP < 10", failed, passed=True)
    assert failed == ["quality"]
    assert "PASS" in capsys.readouterr().out


def test_run_audit_script(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ql.subprocess, "run", lambda *a, **k: calls.append((a, k)) or fake_proc(1))
    failed = []
    ql.run_audit_script("quality", Path("/x/audit.py"), "crap-queue.md", "CRAP < 10", failed)
    out = capsys.readouterr().out
    assert "  running quality: audit.py" in out
    assert "quality: FAIL (gate: CRAP < 10) -> see crap-queue.md" in out
    assert failed == ["quality"]


def test_run_audit_script_pass(monkeypatch):
    monkeypatch.setattr(ql.subprocess, "run", lambda *a, **k: fake_proc(0))
    failed = []
    ql.run_audit_script("quality", Path("/x/audit.py"), "crap-queue.md", "d", failed)
    assert failed == []


def test_run_audit_runs_when_no_blocker(monkeypatch):
    calls = []
    monkeypatch.setattr(ql, "run_audit_script", lambda *a: calls.append(a))
    auds = {"quality": (Path("a.py"), "crap-queue.md", "d", ())}
    failed, skipped = [], []
    ql.run_audit("quality", auds, failed, skipped)
    assert len(calls) == 1 and skipped == []


def test_run_audit_blocked_by_prereq(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ql, "run_audit_script", lambda *a: calls.append(a))
    auds = {"stryker": (Path("s.py"), "stryker-queue.md", "d", ("quality",))}
    failed, skipped = ["quality"], []
    ql.run_audit("stryker", auds, failed, skipped)
    assert calls == []
    assert skipped == ["stryker"]
    assert "skipping stryker: prerequisite gate failed (quality)" in capsys.readouterr().out


def test_verify_iteration_order(monkeypatch):
    seen = []

    def fake_run(name, auds, failed, skipped):
        seen.append(name)
        if name == "quality":
            failed.append(name)

    monkeypatch.setattr(ql, "run_audit", fake_run)
    auds = ql.build_audits("python")
    failed, skipped = ql.verify_iteration(["quality", "metrics"], auds)
    assert seen == ["quality", "metrics"]
    assert failed == ["quality"]
    assert skipped == []


def test_print_all_clean(capsys):
    ql.print_all_clean(["quality"], ql.build_audits("python"))
    out = capsys.readouterr().out
    assert "ALL GATES PASSED" in out and "crap-queue.md: clean" in out


def test_queue_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ql, "REPO", tmp_path)
    assert ql.queue_paths(["quality", "metrics"], ql.build_audits("python")) == (
        f"{tmp_path / 'crap-queue.md'}, {tmp_path / 'metrics-queue.md'}"
    )


def test_print_exhausted(tmp_path, capsys):
    (tmp_path / "one.jsonl").write_text("")
    ql.print_exhausted(["quality"], ql.build_audits("python"), tmp_path, 3)
    out = capsys.readouterr().out
    assert "Implementor sessions kept at:" in out
    assert "Take over the most recent pass with:" in out
    assert "Max iterations (3) reached" in out
    assert "crap-queue.md" in out


def test_print_exhausted_without_sessions(tmp_path, capsys):
    ql.print_exhausted(["quality"], ql.build_audits("python"), tmp_path, 1)
    assert "Take over" not in capsys.readouterr().out


def test_passed_last():
    assert not ql.passed_last([])
    assert ql.passed_last([True])
    assert not ql.passed_last([True, False])
    assert ql.passed_last([False, True])


def test_exit_after_clean(tmp_path, monkeypatch):
    clean, exhausted = [], []
    monkeypatch.setattr(ql, "print_all_clean", lambda *a: clean.append(a))
    monkeypatch.setattr(ql, "print_exhausted", lambda *a: exhausted.append(a))
    config = make_config(tmp_path)
    assert ql.exit_after([False, True], ["quality"], config, 5) == 0
    assert len(clean) == 1 and exhausted == []


def test_exit_after_exhausted(tmp_path, monkeypatch):
    clean, exhausted = [], []
    monkeypatch.setattr(ql, "print_all_clean", lambda *a: clean.append(a))
    monkeypatch.setattr(ql, "print_exhausted", lambda *a: exhausted.append(a))
    config = make_config(tmp_path)
    assert ql.exit_after([False], ["quality"], config, 2) == 1
    assert clean == [] and len(exhausted) == 1
    assert ql.exit_after([], ["quality"], config, 2) == 1


def test_print_skipped(capsys):
    ql.print_skipped([])
    assert capsys.readouterr().out == ""
    ql.print_skipped(["stryker"])
    assert "not audited this iteration (prerequisite failed): stryker" in capsys.readouterr().out


def test_iteration_passes_without_implementor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ql, "verify_iteration", lambda enabled, auds: ([], []))
    runs = []
    monkeypatch.setattr(ql, "run_implementor", lambda *a: runs.append(a))
    config = make_config(tmp_path)
    args = Namespace(max_iterations=3, dry_run=False)
    assert ql.iteration(1, ["quality", "metrics"], config, args) is True
    assert runs == []
    # The pass summary is exit_after()'s job; iteration only reports the
    # per-audit PASS lines from verify_iteration.
    assert "ALL GATES PASSED" not in capsys.readouterr().out


def test_iteration_failed_launches_implementor(tmp_path, monkeypatch):
    monkeypatch.setattr(ql, "verify_iteration", lambda enabled, auds: (["quality"], []))
    runs = []
    monkeypatch.setattr(ql, "run_implementor", lambda config, failed, dry_run: runs.append((config, failed, dry_run)))
    config = make_config(tmp_path)
    args = Namespace(max_iterations=3, dry_run=True)
    assert ql.iteration(1, ["quality"], config, args) is False
    assert runs == [(config, ["quality"], True)]


def test_iteration_stream_and_run_iterations(monkeypatch, tmp_path):
    monkeypatch.setattr(ql, "iteration", lambda i, e, c, a: [False, False, True][i - 1])
    config = make_config(tmp_path)
    args = Namespace(max_iterations=3, dry_run=False)
    assert list(ql.iteration_stream(["quality"], config, args)) == [False, False, True]

    # The passing iteration IS included; the generator stops right after it,
    # so exit_after sees the True and exits 0 (see run_iterations docstring).
    monkeypatch.setattr(ql, "iteration", lambda i, e, c, a: [False, True, True][i - 1])
    assert list(ql.run_iterations(["quality"], config, args)) == [False, True]

    # First-iteration pass also surfaces a True.
    monkeypatch.setattr(ql, "iteration", lambda i, e, c, a: True)
    assert list(ql.run_iterations(["quality"], config, args)) == [True]


def test_run_loop_returns_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ql, "run_iterations", lambda enabled, config, args: [False, False, True])
    config = make_config(tmp_path)
    args = Namespace(max_iterations=5, dry_run=False)
    assert ql.run_loop(["quality"], config, args) == 0
    assert "ALL GATES PASSED" in capsys.readouterr().out


def test_run_loop_exhausts(tmp_path, monkeypatch):
    monkeypatch.setattr(ql, "run_iterations", lambda enabled, config, args: [False])
    config = make_config(tmp_path)
    args = Namespace(max_iterations=5, dry_run=False)
    assert ql.run_loop(["quality"], config, args) == 1


# ------------------------------------------------------------- config / main

def test_build_config_env_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("QUALITY_MODEL", "m1")
    monkeypatch.setenv("QUALITY_SESSION_DIR", str(tmp_path / "s"))
    config = ql.build_config(Namespace(batch_size=4), "python", {"a": 1})
    assert config.stack == "python"
    assert config.batch == 4


def test_build_config_env_connected(monkeypatch, tmp_path):
    monkeypatch.setenv("QUALITY_MODEL", "m1")
    monkeypatch.setenv("QUALITY_SESSION_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("QUALITY_PI_APPROVE", "0")
    config = ql.build_config(Namespace(batch_size=4), "python", {"a": 1})
    assert config.model == "m1"
    assert config.session_dir == tmp_path / "s"
    assert config.auds == {"a": 1}


def test_build_config_default_approve(monkeypatch):
    monkeypatch.delenv("QUALITY_APPROVE", raising=False)
    monkeypatch.delenv("QUALITY_PI_APPROVE", raising=False)
    monkeypatch.delenv("QUALITY_MODEL", raising=False)
    config = ql.build_config(Namespace(batch_size=1), "dotnet", {})
    assert config.approve is True
    assert config.model == ""


def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quality-loop.py"])
    args = ql.parse_args()
    assert args.max_iterations == 10
    assert args.batch_size == 5
    assert args.skip == []
    assert not args.dry_run
    assert not args.report


def test_parse_args_positional_and_flags(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quality-loop.py", "3", "7", "--skip", "metrics", "--dry-run"])
    args = ql.parse_args()
    assert args.max_iterations == 3
    assert args.batch_size == 7
    assert args.skip == ["metrics"]
    assert args.dry_run
    assert not args.report


def test_parse_args_report_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quality-loop.py", "5", "--report"])
    args = ql.parse_args()
    assert args.report and not args.dry_run


def test_parse_args_report_conflicts_with_dry_run(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quality-loop.py", "--report", "--dry-run"])
    with pytest.raises(SystemExit):
        ql.parse_args()


def test_main_runs_loop(monkeypatch, tmp_path, capsys):
    seen = []

    def fake_detect(repo):
        seen.append(("detect", repo))
        return "python"

    monkeypatch.setattr(ql, "detect_stack", fake_detect)
    monkeypatch.setattr(ql, "build_audits", lambda stack: {"quality": (Path("a.py"), "crap-queue.md", "d", ())})
    monkeypatch.setattr(ql, "validate_skips", lambda skip, auds, stack: None)
    monkeypatch.setattr(ql, "run_loop", lambda enabled, config, args: 7)
    monkeypatch.setenv("QUALITY_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["quality-loop.py", "2", "3"])
    assert ql.main() == 7
    assert seen == [("detect", ql.REPO)]
    assert (tmp_path / "sessions").is_dir()
    out = capsys.readouterr().out
    assert "Detected stack: python (quality audits enabled; repo" in out
    assert "Implementor harness:" in out


# ------------------------------------------------------------- report mode

def test_run_report_audits_records_statuses(monkeypatch, capsys):
    codes = {"a.py": 0, "b.py": 1}

    def fake_run(cmd, **kw):
        return fake_proc(codes[Path(cmd[-1]).name])

    monkeypatch.setattr(ql.subprocess, "run", fake_run)
    auds = {"quality": (Path("a.py"), "crap-queue.md", "d", ()),
            "metrics": (Path("b.py"), "metrics-queue.md", "d", ())}
    runs = ql.run_report_audits(["quality", "metrics"], auds)
    assert [(r.name, r.status, r.detail) for r in runs] == [
        ("quality", "passed", "exit 0"), ("metrics", "failed", "exit 1")]
    assert all(r.started is not None for r in runs)
    out = capsys.readouterr().out
    assert "quality: PASS" in out
    assert "metrics: FAIL (gate: d) -> see metrics-queue.md" in out


def test_run_report_audits_skips_blocked_prereq(monkeypatch, capsys):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(Path(cmd[-1]).name)
        return fake_proc(1)  # quality fails, so the stryker prereq blocks

    monkeypatch.setattr(ql.subprocess, "run", fake_run)
    auds = {"quality": (Path("a.py"), "q.md", "d", ()),
            "stryker": (Path("s.py"), "s.md", "d", ("quality",))}
    runs = ql.run_report_audits(["quality", "stryker"], auds)
    assert [(r.name, r.status) for r in runs] == [("quality", "failed"), ("stryker", "skipped")]
    assert runs[1].started is None and "prerequisite gate failed: quality" in runs[1].detail
    assert calls == ["a.py"]
    assert "skipping stryker: prerequisite gate failed (quality)" in capsys.readouterr().out


def test_print_report_result_red(tmp_path, capsys):
    ticket = qr.Ticket(key="t1", title="x", type="task", priority=1, gate="quality", file="a.py")
    result = qr.ReportResult(report_path=tmp_path / "quality-report.md",
                             plan_path=tmp_path / "quality-plan.json",
                             tickets=[ticket], verify=None, failed=["quality"],
                             notes=["quality: no crap-report.json"],
                             waves=[[ticket]])
    ql.print_report_result(result)
    out = capsys.readouterr().out
    assert "1 ticket(s) (+1 verification) across 1 wave(s); 1 ready now" in out
    assert "NOTE: quality: no crap-report.json" in out
    assert "import: bd create --graph quality-plan.json" in out


def test_print_report_result_clean(tmp_path, capsys):
    result = qr.ReportResult(report_path=tmp_path / "quality-report.md", plan_path=None,
                             tickets=[], verify=None, failed=[], notes=[], waves=[])
    ql.print_report_result(result)
    out = capsys.readouterr().out
    assert "ALL ENABLED GATES PASSED (fresh) — nothing to ticket" in out
    assert str(result.report_path) in out


def test_run_report_mode_red(tmp_path, monkeypatch, capsys):
    ticket = qr.Ticket(key="t1", title="x", type="task", priority=1, gate="quality", file="a.py")
    result = qr.ReportResult(report_path=tmp_path / "quality-report.md",
                             plan_path=tmp_path / "quality-plan.json",
                             tickets=[ticket], verify=None, failed=["quality"], notes=[], waves=[[ticket]])
    monkeypatch.setattr(ql, "run_report_audits",
                        lambda enabled, auds: [qr.AuditRun("quality", "failed", 1.0, "exit 1")])
    monkeypatch.setattr(ql, "write_report_and_plan", lambda *a, **k: result)
    assert ql.run_report_mode(["quality"], make_config(tmp_path)) == 1
    assert "REPORT MODE" in capsys.readouterr().out


def test_run_report_mode_green(tmp_path, monkeypatch, capsys):
    result = qr.ReportResult(report_path=tmp_path / "quality-report.md", plan_path=None,
                             tickets=[], verify=None, failed=[], notes=[], waves=[])
    monkeypatch.setattr(ql, "run_report_audits",
                        lambda enabled, auds: [qr.AuditRun("quality", "passed", 1.0, "exit 0")])
    monkeypatch.setattr(ql, "write_report_and_plan", lambda *a, **k: result)
    assert ql.run_report_mode(["quality"], make_config(tmp_path)) == 0
    assert "ALL ENABLED GATES PASSED" in capsys.readouterr().out


def test_main_report_mode_skips_session_dir(monkeypatch, tmp_path, capsys):
    seen = []
    monkeypatch.setattr(ql, "detect_stack", lambda repo: "python")
    monkeypatch.setattr(ql, "build_audits",
                        lambda stack: {"quality": (Path("a.py"), "crap-queue.md", "d", ())})
    monkeypatch.setattr(ql, "validate_skips", lambda skip, auds, stack: None)
    monkeypatch.setattr(ql, "run_report_mode", lambda enabled, config: seen.append(enabled) or 7)
    monkeypatch.setenv("QUALITY_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["quality-loop.py", "--report"])
    assert ql.main() == 7
    assert seen == [["quality"]]
    assert not (tmp_path / "sessions").exists()
    out = capsys.readouterr().out
    assert "Implementor harness:" not in out