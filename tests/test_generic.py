"""Retirement and genericity: any-mcp ships no platform-specific plugin."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return out.splitlines()


def test_orchestrator_plugin_retired() -> None:
    tracked = set(_tracked())
    for path in (
        "src/any_mcp/orchestrator.py",
        "tests/test_orchestrator_plugin.py",
        "tests/test_nomenclature.py",
    ):
        assert path not in tracked, path


def test_no_platform_names() -> None:
    offenders = []
    for rel in _tracked():
        if not rel.startswith(("src/", "examples/", "docs/")):
            continue
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore").lower()
        offenders += [f"{rel}: {n}" for n in ("/agent/v1", "orchestrator", "hive") if n in text]
    assert not offenders, offenders


def test_no_orchestrator_alias() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("any_mcp.orchestrator")


def test_readme_plugin_sources_section() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Orchestrator plugin" not in readme
    start = readme.index("## Plugin sources\n")
    rest = readme[start + 1 :]
    end = rest.find("\n## ")
    section = rest if end == -1 else rest[:end]
    for needle in (
        "git::",
        "github.com/",
        "./",
        "../",
        "any-mcp plugins install",
        "any-mcp-tatara-plugins",
        "source:",
    ):
        assert needle in section, needle
