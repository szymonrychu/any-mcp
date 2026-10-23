"""Release packaging: versions, the release workflow and every supported install path."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PINNED_LINE = (
    'python -m venv /opt/any-mcp && /opt/any-mcp/bin/pip install '
    '"any-mcp @ git+https://github.com/szymonrychu/any-mcp@v0.1.0"'
)
UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(UV is None, reason="uv is required")


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _run(cmd: list[str | Path], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True, check=True, timeout=600, **kwargs
    )


def _build_wheel(out: Path) -> Path:
    _run([UV, "build", "--wheel", "--out-dir", out], cwd=ROOT)
    wheels = list(out.glob("any_mcp-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _commit_head_url() -> str:
    """git+file URL of HEAD; the install tests need the work committed."""
    return f"git+file://{ROOT}@HEAD"


def test_versions_agree():
    v = _pyproject_version()
    assert v == "0.1.0"
    assert dist_version("any-mcp") == v
    changelog = (ROOT / "CHANGELOG.md").read_text()
    top = re.search(r"^## (\S+)", changelog, re.M)
    assert top is not None and top.group(1) == f"v{v}"
    readme = (ROOT / "README.md").read_text()
    assert f"szymonrychu/any-mcp@v{v}" in readme


def test_release_workflow_shape():
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert re.search(r"push:\s*\n\s*tags:\s*\n\s*- \"?v\*\.\*\.\*\"?", text)
    assert "pyproject.toml" in text and "GITHUB_REF_NAME" in text and 'v${version}' in text
    assert "mise run lint" in text and "mise run test" in text
    assert "uv build --wheel" in text
    assert "any-mcp @ git+https://github.com/${{ github.repository }}@${{ github.ref_name }}" in text
    assert text.count("any-mcp --version") + text.count("any-mcp\" --version") >= 2
    assert text.count("--help") >= 2
    assert "gh release create" in text and "CHANGELOG.md" in text


@needs_uv
def test_wheel_installs_and_reports_version(tmp_path):
    wheel = _build_wheel(tmp_path / "dist")
    venv = tmp_path / "venv"
    _run([UV, "venv", venv])
    _run([UV, "pip", "install", "--python", venv, wheel])
    out = _run([venv / "bin" / "any-mcp", "--version"]).stdout
    assert out.strip() == "any-mcp 0.1.0"


@needs_uv
def test_install_from_git_head(tmp_path):
    venv = tmp_path / "venv"
    _run([UV, "venv", venv])
    _run([UV, "pip", "install", "--python", venv, f"any-mcp @ {_commit_head_url()}"])
    out = _run([venv / "bin" / "any-mcp", "--help"]).stdout
    assert "any-mcp" in out


@needs_uv
def test_uv_tool_install(tmp_path):
    wheel = _build_wheel(tmp_path / "dist")
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
    }
    _run([UV, "tool", "install", wheel], env=env)
    out = _run([tmp_path / "bin" / "any-mcp", "--version"], env=env).stdout
    assert out.strip() == "any-mcp 0.1.0"


def test_onstart_install_path(tmp_path):
    venv = tmp_path / "any-mcp"
    _run([sys.executable, "-m", "venv", venv])
    _run([venv / "bin" / "pip", "install", f"any-mcp @ {_commit_head_url()}"])
    config = tmp_path / "any-mcp.yaml"
    config.write_text(
        json.dumps(
            {
                "python_path": [str(ROOT / "examples" / "hello")],
                "plugins": [{"name": "hello", "plugin": "hello:Hello"}],
            }
        )
    )
    out = _run([venv / "bin" / "any-mcp", "--config", config, "--help"]).stdout
    assert "any-mcp" in out
    code = (
        "import sys; from any_mcp.config import load_config; "
        "cfg = load_config(sys.argv[1]); print(cfg.plugins[0].name)"
    )
    loaded = _run([venv / "bin" / "python", "-c", code, config]).stdout
    assert loaded.strip() == "hello"


def test_pinned_install_line_documented():
    for name in ("README.md", "CHANGELOG.md"):
        text = (ROOT / name).read_text()
        assert PINNED_LINE in text, name
        assert "/opt/any-mcp/bin/any-mcp" in text, name
        assert re.search(r"config file may be JSON", text), name
