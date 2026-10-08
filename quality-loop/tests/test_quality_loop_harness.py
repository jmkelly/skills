"""Tests for scripts/quality-loop.py — harness support (pi | opencode).

Covers harness resolution (CLI flag > QUALITY_HARNESS > default pi),
per-harness session-dir bases, the `opencode run` command construction /
launch path, and the harness-aware exhaustion report.
"""
from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.conftest import QUALITY_LOOP as ql
from tests.conftest import fake_proc


def make_config(tmp_path: Path, **kw) -> ql.ImplementorConfig:
    defaults = dict(stack="python", batch=2, session_dir=tmp_path / "sessions",
                    model="", approve=True, auds=ql.build_audits("python"))
    defaults.update(kw)
    return ql.ImplementorConfig(**defaults)


# ------------------------------------------------------- harness resolution

def test_resolve_harness_default_is_pi(monkeypatch):
    monkeypatch.delenv("QUALITY_HARNESS", raising=False)
    assert ql.resolve_harness(None) == "pi"


def test_resolve_harness_from_env(monkeypatch):
    monkeypatch.setenv("QUALITY_HARNESS", "opencode")
    assert ql.resolve_harness(None) == "opencode"


def test_resolve_harness_cli_beats_env(monkeypatch):
    monkeypatch.setenv("QUALITY_HARNESS", "opencode")
    assert ql.resolve_harness("pi") == "pi"


def test_resolve_harness_case_insensitive(monkeypatch):
    monkeypatch.delenv("QUALITY_HARNESS", raising=False)
    assert ql.resolve_harness("OpenCode") == "opencode"


def test_resolve_harness_unknown_raises(monkeypatch):
    monkeypatch.delenv("QUALITY_HARNESS", raising=False)
    with pytest.raises(SystemExit, match="unknown harness"):
        ql.resolve_harness("cursor")


def test_resolve_harness_unknown_env_raises(monkeypatch):
    monkeypatch.setenv("QUALITY_HARNESS", "cursor")
    with pytest.raises(SystemExit, match="unknown harness"):
        ql.resolve_harness(None)


# ------------------------------------------------------------- session dirs

def test_default_session_base_per_harness():
    assert ql.default_session_base("pi") == Path.home() / ".pi" / "sessions" / "quality-implementor"
    assert ql.default_session_base("opencode") == (
        Path.home() / ".local" / "share" / "opencode" / "sessions" / "quality-implementor"
    )


def test_session_dir_from_env_default_pi(monkeypatch):
    monkeypatch.delenv("QUALITY_SESSION_DIR", raising=False)
    assert ql.session_dir_from_env().parent == ql.default_session_base("pi")
    assert ql.session_dir_from_env("pi").parent == ql.default_session_base("pi")


def test_session_dir_from_env_opencode_unique(monkeypatch):
    monkeypatch.delenv("QUALITY_SESSION_DIR", raising=False)
    first = ql.session_dir_from_env("opencode")
    second = ql.session_dir_from_env("opencode")
    assert first.parent == ql.default_session_base("opencode")
    assert first != second


def test_session_dir_from_env_override_wins_for_both_harnesses(monkeypatch, tmp_path):
    monkeypatch.setenv("QUALITY_SESSION_DIR", str(tmp_path / "s"))
    assert ql.session_dir_from_env("pi") == tmp_path / "s"
    assert ql.session_dir_from_env("opencode") == tmp_path / "s"


# ------------------------------------------------------- opencode plumbing

def test_build_opencode_command_full():
    config = make_config(Path("/s"), harness="opencode", model="m1",
                         agent="build", approve=True, session_dir=Path("/sess"))
    assert ql.build_opencode_command(config, "fix it") == [
        "opencode", "run", "--model", "m1", "--agent", "build",
        "--auto", "--title", "quality-implementor", "fix it",
    ]


def test_build_opencode_command_minimal():
    config = make_config(Path("/s"), harness="opencode", model="",
                         agent="", approve=False)
    cmd = ql.build_opencode_command(config, "b")
    assert cmd == ["opencode", "run", "--title", "quality-implementor", "b"]


def test_build_harness_command_dispatches(tmp_path):
    pi_config = make_config(tmp_path, harness="pi")
    assert ql.build_harness_command(pi_config, "b")[0] == "pi"
    assert ql.build_harness_command(pi_config, "b")[1] != "run"
    oc_config = make_config(tmp_path, harness="opencode")
    assert ql.build_harness_command(oc_config, "b")[:2] == ["opencode", "run"]


def test_run_opencode(monkeypatch):
    calls = []
    monkeypatch.setattr(ql.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or fake_proc())
    ql.run_opencode(["opencode", "run", "x"])
    cmd, kw = calls[0]
    assert cmd == ["opencode", "run", "x"]
    assert kw["cwd"] == ql.REPO
    assert kw["check"] is False


def test_run_opencode_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(ql.subprocess, "run", boom)
    with pytest.raises(SystemExit, match="'opencode' not found on PATH"):
        ql.run_opencode(["opencode"])


def test_run_harness_dispatches(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ql, "run_pi", lambda cmd: seen.append(("pi", cmd)))
    monkeypatch.setattr(ql, "run_opencode", lambda cmd: seen.append(("opencode", cmd)))
    ql.run_harness(make_config(tmp_path, harness="pi"), ["pi"])
    ql.run_harness(make_config(tmp_path, harness="opencode"), ["opencode"])
    assert [s for s, _ in seen] == ["pi", "opencode"]


def test_launch_uses_harness_label_and_runner(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ql, "run_harness", lambda config, cmd: calls.append((config.harness, cmd)))
    ql.launch(make_config(tmp_path, harness="opencode"), ["opencode", "run", "x"])
    out = capsys.readouterr().out
    assert "opencode command: opencode run x" in out
    assert calls == [("opencode", ["opencode", "run", "x"])]


def test_run_implementor_launches_opencode(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(ql, "launch", lambda config, cmd: launched.append(cmd))
    ql.run_implementor(make_config(tmp_path, harness="opencode"), ["metrics"], dry_run=False)
    assert len(launched) == 1
    assert launched[0][:2] == ["opencode", "run"]


def test_print_exhausted_opencode(tmp_path, capsys):
    ql.print_exhausted(["quality"], ql.build_audits("python"), tmp_path, 3, harness="opencode")
    out = capsys.readouterr().out
    assert "Implementor handoff kept at:" in out
    assert "Take over" not in out
    assert "Max iterations (3) reached" in out


def test_brief_session_note_differs_per_harness(tmp_path):
    pi_brief = ql.build_brief(make_config(tmp_path, harness="pi"), ["quality"])
    oc_brief = ql.build_brief(make_config(tmp_path, harness="opencode"), ["quality"])
    assert "JSONL" in pi_brief
    assert "JSONL" not in oc_brief
    assert "handoff summary above is your only memory" in oc_brief
    # shared contract: batch, queues, handoff path survive per-harness wording
    for brief in (pi_brief, oc_brief):
        assert "Refactor the worst 2 offenders" in brief
        assert "- crap-queue.md (failing gate:" in brief


# ------------------------------------------------------------- config / args

def test_build_config_harness_and_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("QUALITY_HARNESS", "opencode")
    monkeypatch.setenv("QUALITY_AGENT", "build")
    monkeypatch.setenv("QUALITY_SESSION_DIR", str(tmp_path / "s"))
    config = ql.build_config(Namespace(batch_size=4), "python", {"a": 1})
    assert config.harness == "opencode"
    assert config.agent == "build"
    assert config.session_dir == tmp_path / "s"


def test_build_config_harness_defaults_without_flag(monkeypatch):
    monkeypatch.delenv("QUALITY_HARNESS", raising=False)
    monkeypatch.delenv("QUALITY_AGENT", raising=False)
    # Namespace without a harness attr (old callers) still builds a pi config.
    config = ql.build_config(Namespace(batch_size=1), "dotnet", {})
    assert config.harness == "pi"
    assert config.agent == ""


def test_build_config_approve_envs(monkeypatch):
    monkeypatch.delenv("QUALITY_APPROVE", raising=False)
    monkeypatch.setenv("QUALITY_PI_APPROVE", "0")
    assert ql.build_config(Namespace(batch_size=1), "dotnet", {}).approve is False
    monkeypatch.setenv("QUALITY_APPROVE", "1")
    assert ql.build_config(Namespace(batch_size=1), "dotnet", {}).approve is True


def test_parse_args_harness_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quality-loop.py", "--harness", "opencode"])
    assert ql.parse_args().harness == "opencode"


def test_parse_args_harness_defaults_none(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quality-loop.py"])
    assert ql.parse_args().harness is None
