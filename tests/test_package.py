import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_version_from_metadata():
    import any_mcp

    assert any_mcp.__version__ == "0.5.0"


def test_pyproject_declares_dependencies_and_script():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = data["project"]
    assert project["name"] == "any-mcp"
    assert project["requires-python"] == ">=3.12"
    assert project["scripts"] == {"any-mcp": "any_mcp.cli:main"}
    assert sorted(project["dependencies"]) == sorted(
        ["fastmcp>=4.1,<5", "pydantic>=2", "jsonschema>=4.23", "pyyaml", "httpx"]
    )
    assert sorted(data["dependency-groups"]["dev"]) == sorted(
        ["pytest>=8.3", "pytest-asyncio", "ruff"]
    )
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/any_mcp"]


def test_mise_tasks_defined():
    tasks = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
    assert tasks["install"]["run"] == "uv sync"
    assert tasks["test"]["run"] == "uv run pytest"
    assert tasks["lint"]["run"] == "uv run ruff check . && uv run ruff format --check ."
    assert tasks["fmt"]["run"] == "uv run ruff format . && uv run ruff check --fix ."


def test_ci_workflow_runs_lint_and_test():
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = wf.get("on", wf.get(True))
    assert "push" in triggers and "pull_request" in triggers
    runs = [s["run"] for job in wf["jobs"].values() for s in job["steps"] if "run" in s]
    assert "mise run install" in runs
    assert "mise run lint" in runs
    assert "mise run test" in runs
    uses = [s["uses"] for job in wf["jobs"].values() for s in job["steps"] if "uses" in s]
    assert any(u.startswith("jdx/mise-action@") for u in uses)


def test_python_version_pinned_to_312():
    pinned = (ROOT / ".python-version").read_text().strip()
    mise = tomllib.loads((ROOT / "mise.toml").read_text())
    assert pinned == "3.12"
    assert mise["tools"]["python"] == pinned


def test_interpreter_is_312():
    import sys

    assert sys.version_info[:2] == (3, 12)
