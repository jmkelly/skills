"""Shared fixtures for the quality-loop skill test suite.

The audited modules are plain scripts (no package) that resolve their repo
root at import time via git, so tests import them through the ``scripts.*``
namespace package with the repo root on sys.path, and monkeypatch each
module's REPO / REPORT / QUEUE globals to tmp_path to keep audit artifacts
out of the real skill tree.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, path: Path):
    """Load a dash-named script (not importable via `import`)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# scripts/python/metrics-audit.py and scripts/quality-loop.py contain dashes,
# so they cannot be imported by name; load them from file like the audits do.
METRICS_AUDIT = _load_module("quality_loop_skill.metrics_audit", REPO_ROOT / "scripts" / "python" / "metrics-audit.py")
QUALITY_LOOP = _load_module("quality_loop_skill.quality_loop", REPO_ROOT / "scripts" / "quality-loop.py")
DOTNET_METRICS = _load_module("quality_loop_skill.dotnet_metrics_audit", REPO_ROOT / "scripts" / "dotnet" / "metrics-audit.py")
DOTNET_STRYKER = _load_module("quality_loop_skill.dotnet_stryker_audit", REPO_ROOT / "scripts" / "dotnet" / "stryker-audit.py")


def fake_proc(returncode: int = 0, stdout: str = "out\n", stderr: str = "err\n") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)