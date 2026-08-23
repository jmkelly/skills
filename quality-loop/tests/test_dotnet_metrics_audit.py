"""Tests for scripts/dotnet/metrics-audit.py — .NET code-metrics gate.

Covers gate-config selection (repo `.dependably` vs the skill-bundled default),
queue rendering, and the exit-code mapping, with codemetrics faked out.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.conftest import DOTNET_METRICS as dm


def run_main(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["metrics-audit.py", *args])
    return dm.main()


def sample_report() -> dict:
    """Minimal codemetrics JSON matching the schema queue_md() consumes."""
    return {
        "tool": "codemetrics",
        "toolVersion": "1.2.3",
        "schemaVersion": "1.0",
        "target": ".",
        "extra": {
            "Meta": {"ToolVersion": "1.2.3"},
            "metrics": {
                "Summary": {
                    "Files": 2, "Types": 2, "Methods": 4, "TotalSloc": 120,
                    "AverageCyclomatic": 12, "AverageCognitive": 8,
                    "AverageMaintainabilityIndex": 45, "MaxCyclomatic": 30, "MaxCognitive": 20,
                },
                "Methods": [
                    {"MaintainabilityIndex": 10, "Cyclomatic": 30, "Cognitive": 20, "Sloc": 60,
                     "Type": "Bad.Service", "Name": "Handle", "File": "Bad/Service.cs", "StartLine": 3},
                    {"MaintainabilityIndex": 90, "Cyclomatic": 2, "Cognitive": 1, "Sloc": 5,
                     "Type": "Good", "Name": "Run", "File": "Good.cs", "StartLine": 1},
                ],
            },
        },
        "summary": {
            "scanned": 17,
            "findings": 3,
            "bySeverity": {"critical": 1, "high": 1, "moderate": 1, "low": 0, "info": 0},
        },
        "findings": [
            {"severity": "high", "ruleId": "cyclomatic", "message": "cc 30",
             "location": {"file": "Bad/Service.cs", "line": 3}, "remediation": "extract"},
            {"severity": "critical", "ruleId": "mi", "message": "MI 10",
             "location": {"file": "Bad/Service.cs", "line": 2}, "remediation": "split"},
            {"severity": "moderate", "ruleId": "nesting", "message": "depth 6",
             "location": {"file": None, "line": None}, "remediation": "guard clauses"},
        ],
    }


def fake_run(report: dict, returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=json.dumps(report), stderr="")


# ---------------------------------------------------------------- gate config

def test_gate_config_prefers_repo_dependably(tmp_path, monkeypatch):
    (tmp_path / ".dependably").write_text("{}")
    monkeypatch.setattr(dm, "REPO", tmp_path)
    assert dm.gate_config() == tmp_path / ".dependably"


def test_gate_config_falls_back_to_bundled_default(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "REPO", tmp_path)
    assert dm.gate_config() == dm.DEFAULT_CONFIG
    assert dm.DEFAULT_CONFIG.name == ".dependably.default"
    assert dm.DEFAULT_CONFIG.exists()


def test_gate_config_raises_when_default_missing(tmp_path, monkeypatch):
    missing = tmp_path / "nope"
    monkeypatch.setattr(dm, "REPO", tmp_path)
    monkeypatch.setattr(dm, "DEFAULT_CONFIG", missing)
    with pytest.raises(SystemExit):
        dm.gate_config()


# ---------------------------------------------------------------- queue render

def test_queue_md_findings_worst_first(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "REPO", tmp_path)
    md = dm.queue_md(sample_report())
    lines = [l for l in md.splitlines() if l.startswith("- **")]
    assert "**critical**" in lines[0]
    assert "**high**" in lines[1]
    assert "**moderate**" in lines[2]
    assert "MI    10" in md and "cc 30" in md
    assert md.index("MI    10") < md.index("MI    90")


# ---------------------------------------------------------------- main/gate

def test_main_passes_config_and_gates_on_exit_code(tmp_path, monkeypatch):
    calls = {}
    (tmp_path / ".dependably").write_text("{}")
    monkeypatch.setattr(dm, "REPO", tmp_path)
    monkeypatch.setattr(dm, "REPORT", tmp_path / "metrics-report.json")
    monkeypatch.setattr(dm, "QUEUE", tmp_path / "metrics-queue.md")

    def run(cmd, **kw):
        calls["cmd"] = cmd
        return fake_run(sample_report(), returncode=1)

    monkeypatch.setattr(dm.subprocess, "run", run)
    assert run_main(monkeypatch) == 1
    assert calls["cmd"][0] == dm.find_tool("codemetrics")
    assert str(tmp_path) in calls["cmd"]
    assert "--config" in calls["cmd"]
    assert calls["cmd"][calls["cmd"].index("--config") + 1] == str(tmp_path / ".dependably")
    assert (tmp_path / "metrics-report.json").exists()
    assert (tmp_path / "metrics-queue.md").exists()


def test_main_no_gate_always_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "REPO", tmp_path)
    monkeypatch.setattr(dm, "REPORT", tmp_path / "metrics-report.json")
    monkeypatch.setattr(dm, "QUEUE", tmp_path / "metrics-queue.md")
    monkeypatch.setattr(dm.subprocess, "run", lambda *a, **k: fake_run(sample_report(), returncode=1))
    assert run_main(monkeypatch, "--no-gate") == 0


def test_main_uses_bundled_default_when_repo_has_no_dependably(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(dm, "REPO", tmp_path)
    monkeypatch.setattr(dm, "REPORT", tmp_path / "metrics-report.json")
    monkeypatch.setattr(dm, "QUEUE", tmp_path / "metrics-queue.md")

    def run(cmd, **kw):
        calls["cmd"] = cmd
        return fake_run(sample_report(), returncode=0)

    monkeypatch.setattr(dm.subprocess, "run", run)
    assert run_main(monkeypatch) == 0
    cfg = calls["cmd"][calls["cmd"].index("--config") + 1]
    assert cfg == str(dm.DEFAULT_CONFIG)


def test_main_invalid_json_is_usage_error(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "REPO", tmp_path)
    monkeypatch.setattr(dm, "REPORT", tmp_path / "metrics-report.json")
    monkeypatch.setattr(dm, "QUEUE", tmp_path / "metrics-queue.md")
    proc = subprocess.CompletedProcess([], 2, stdout="not json", stderr="boom")
    monkeypatch.setattr(dm.subprocess, "run", lambda *a, **k: proc)
    assert run_main(monkeypatch) == 2


def test_main_missing_tool_returns_2(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "find_tool", lambda name: None)
    assert run_main(monkeypatch) == 2