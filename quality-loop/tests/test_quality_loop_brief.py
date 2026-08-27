"""Tests for scripts/quality-loop.py — implementor brief and pi plumbing.

Covers session-dir / handoff-file handling, assembly of the implementor
brief (role text, per-suite guidance, previous-pass handoff), and the pi
command construction / launch path (model and approve flags, dry-run).
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from tests.conftest import QUALITY_LOOP as ql
from tests.conftest import fake_proc


def make_config(tmp_path: Path, **kw) -> ql.ImplementorConfig:
    defaults = dict(stack="python", batch=2, session_dir=tmp_path / "sessions",
                    model="", approve=True, auds=ql.build_audits("python"))
    defaults.update(kw)
    return ql.ImplementorConfig(**defaults)


# ------------------------------------------------------------- config/env

def test_session_dir_from_env_default():
    base = Path.home() / ".pi" / "sessions" / "quality-implementor"
    first = ql.session_dir_from_env()
    second = ql.session_dir_from_env()
    assert first.parent == base
    # unique per run: repo slug + start timestamp
    assert re.fullmatch(rf"{re.escape(ql.REPO.name)}-\d{{8}}-\d{{6}}-\d{{6}}", first.name)
    assert first != second


def test_session_dir_from_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("QUALITY_SESSION_DIR", str(tmp_path / "s"))
    assert ql.session_dir_from_env() == tmp_path / "s"


def test_wrap_previous_empty():
    assert ql.wrap_previous("") == ""


def test_wrap_previous_short():
    out = ql.wrap_previous("did stuff")
    assert "Handoff from the previous implementor pass" in out
    assert "did stuff" in out
    assert out.endswith("----- end previous pass summary -----\n")


def test_wrap_previous_truncates_long():
    out = ql.wrap_previous("x" * 5000)
    assert out.count("x") == 4000


def test_previous_handoff_missing(tmp_path):
    assert ql.previous_handoff(tmp_path / "implementor-summary.txt") == ""


def test_previous_handoff_reads_and_strips(tmp_path):
    handoff = tmp_path / "implementor-summary.txt"
    handoff.write_text("  brief summary  ")
    out = ql.previous_handoff(handoff)
    assert "brief summary" in out
    assert "----- previous pass summary -----" in out


# ------------------------------------------------------------- brief build

def test_queue_links():
    auds = ql.build_audits("python")
    links = ql.queue_links(["quality", "metrics"], auds)
    assert links.splitlines()[0] == "- crap-queue.md (failing gate: CRAP < 10 for every function (radon cc x coverage.py))"
    assert links.splitlines()[1].startswith("- metrics-queue.md")


def test_guidance_lines():
    lines = ql.guidance_lines(["quality"], ql.PYTHON_GUIDANCE)
    assert len(lines) == 1 and lines[0].startswith("- crap-queue.md:")
    assert "warnings" in ql.PYTHON_GUIDANCE and "warnings" in ql.DOTNET_GUIDANCE
    assert "NoWarn" in ql.DOTNET_GUIDANCE["warnings"]
    assert "pyflakes" in ql.PYTHON_GUIDANCE["warnings"]


def test_guidance_for_python():
    data_files, test_cmd, guidance = ql.guidance_for("python")
    assert data_files == ql.PYTHON_DATA_FILES
    assert test_cmd == "run the full test suite (pytest)"
    assert guidance is ql.PYTHON_GUIDANCE


def test_guidance_for_dotnet():
    data_files, test_cmd, guidance = ql.guidance_for("dotnet")
    assert test_cmd == "run the full test suite (dotnet test; Testcontainers needs Docker)"
    assert guidance is ql.DOTNET_GUIDANCE


def test_build_brief_python_role_and_guidance(tmp_path):
    brief = ql.build_brief(make_config(tmp_path), ["quality", "metrics"])
    assert "implementor in the quality loop for this python repo" in brief
    assert "raise Maintainability Index / lower cyclomatic complexity" in brief
    assert "run the full test suite (pytest)" in brief
    assert "Never add '# pragma: no cover' to hide work." in brief


def test_build_brief_python_queues_and_batch(tmp_path):
    brief = ql.build_brief(make_config(tmp_path), ["quality", "metrics", "warnings"])
    assert "- crap-queue.md (failing gate:" in brief
    assert "- metrics-queue.md (failing gate:" in brief
    assert "- warnings-queue.md (failing gate:" in brief
    assert "crap-report.json / metrics-report.json / warnings-report.json" in brief
    assert "Refactor the worst 2 offenders this pass" in brief
    assert "crap-queue.md, metrics-queue.md, warnings-queue.md" in brief


def test_build_brief_python_handoff_and_summary_path(tmp_path):
    config = make_config(tmp_path)
    brief = ql.build_brief(config, ["quality", "metrics"])
    assert f"to {config.session_dir / 'implementor-summary.txt'}" in brief
    assert "previous pass summary" not in brief


def test_build_brief_includes_handoff(tmp_path):
    handoff = tmp_path / "sessions" / ql.HANDOFF_FILE
    handoff.parent.mkdir(parents=True)
    handoff.write_text("finished item X")
    config = make_config(tmp_path)
    brief = ql.build_brief(config, ["quality"])
    assert "finished item X" in brief
    assert "----- previous pass summary -----" in brief


def test_build_brief_dotnet_batch_order(tmp_path):
    config = make_config(tmp_path, stack="dotnet", auds=ql.build_audits("dotnet"))
    brief = ql.build_brief(config, ["quality", "warnings", "stryker"])
    assert "crap-queue.md, metrics-queue.md, warnings-queue.md, stryker-queue.md" in brief
    assert "Stryker queue:" in brief
    assert "ExcludeFromCodeCoverage" in brief
    assert "NoWarn" in brief


# ------------------------------------------------------------- pi plumbing

def test_model_arg():
    assert ql.model_arg("") == []
    assert ql.model_arg("m1") == ["--model", "m1"]


def test_approve_arg():
    assert ql.approve_arg(True) == ["-a"]
    assert ql.approve_arg(False) == []


def test_build_pi_command():
    config = make_config(Path("/s"), model="m1", approve=True, session_dir=Path("/sess"))
    cmd = ql.build_pi_command(config, "fix it")
    assert cmd == ["pi", "--model", "m1", "-a", "--session-dir", "/sess",
                   "--name", "quality-implementor", "-p", "fix it"]


def test_build_pi_command_minimal():
    config = make_config(Path("/s"), model="", approve=False)
    cmd = ql.build_pi_command(config, "b")
    assert cmd == ["pi", "--session-dir", str(config.session_dir), "--name", "quality-implementor", "-p", "b"]


def test_run_pi(monkeypatch):
    calls = []
    monkeypatch.setattr(ql.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or fake_proc())
    ql.run_pi(["pi", "-p", "x"])
    cmd, kw = calls[0]
    assert cmd == ["pi", "-p", "x"]
    assert kw["cwd"] == ql.REPO
    assert kw["check"] is False


def test_run_pi_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(ql.subprocess, "run", boom)
    with pytest.raises(SystemExit, match="'pi' not found on PATH"):
        ql.run_pi(["pi"])


def test_note_missing_handoff(tmp_path, capsys):
    ql.note_missing_handoff(tmp_path)
    assert "did not write" in capsys.readouterr().out
    (tmp_path / ql.HANDOFF_FILE).write_text("x")
    ql.note_missing_handoff(tmp_path)
    assert capsys.readouterr().out == ""


def test_launch(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ql, "run_pi", lambda cmd: calls.append(cmd))
    config = make_config(tmp_path)
    ql.launch(config, ["pi", "-p", "x"])
    out = capsys.readouterr().out
    assert "pi command: pi -p x" in out
    assert "did not write" in out
    assert calls == [["pi", "-p", "x"]]


def test_launch_with_handoff(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ql, "run_pi", lambda cmd: None)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / ql.HANDOFF_FILE).write_text("summary")
    ql.launch(make_config(tmp_path), ["pi"])
    assert "did not write" not in capsys.readouterr().out


def test_print_dry_run(capsys):
    ql.print_dry_run("line1\nline2")
    out = capsys.readouterr().out
    assert "[dry-run] would run the implementor; brief:" in out
    assert "    line1" in out and "    line2" in out


def test_run_implementor_dry_run(tmp_path, monkeypatch):
    launched = []
    printed = []
    monkeypatch.setattr(ql, "launch", lambda config, cmd: launched.append(cmd))
    monkeypatch.setattr(ql, "print_dry_run", lambda brief: printed.append(brief))
    config = make_config(tmp_path)
    ql.run_implementor(config, ["quality"], dry_run=True)
    assert launched == []
    assert len(printed) == 1 and "Refactor the worst 2 offenders" in printed[0]


def test_run_implementor_launches(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(ql, "launch", lambda config, cmd: launched.append(cmd))
    config = make_config(tmp_path)
    ql.run_implementor(config, ["metrics"], dry_run=False)
    assert len(launched) == 1
    cmd = launched[0]
    assert cmd[0] == "pi"
    assert "--session-dir" in cmd and "--name" in cmd
    assert cmd[-2:] == ["-p", cmd[-1]]