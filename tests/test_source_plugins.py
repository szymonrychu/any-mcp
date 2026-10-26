"""Plugin entries with a ``source``: config rules, install-on-miss loading and the install command.

The in-process tests fake pip and the distribution lookup (nothing is installed into the dev
environment); the end-to-end test installs any-mcp and the fixture plugin into a fresh venv,
offline: uv from its cache, pip with no index.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import EntryPoint, EntryPoints
from pathlib import Path
from types import SimpleNamespace

import pytest

from any_mcp import install as installer
from any_mcp.cli import main
from any_mcp.config import load_config
from any_mcp.discovery import load_plugins
from any_mcp.errors import ConfigError
from any_mcp.install import Installed
from any_mcp.sources import parse_source

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "source_plugin"
GROUP = "any_mcp.plugins"
UV = shutil.which("uv")
assert UV, "uv is a pinned mise tool and must be on PATH"


def write(directory: Path, text: str, name: str = "any-mcp.yaml") -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
        + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "cache"
    monkeypatch.setenv("ANY_MCP_CACHE_DIR", str(path))
    return path


# ---------------------------------------------------------------- ac1


def test_entry_source_validation(tmp_path):
    ok = load_config(
        write(tmp_path, "plugins:\n  - name: a\n    source: ./pkg\n"),
        env={},
    )
    assert ok.plugins[0].source == "./pkg" and ok.plugins[0].plugin is None

    picked = load_config(
        write(tmp_path, "plugins:\n  - name: a\n    source: ./pkg\n    plugin: second\n"),
        env={},
    )
    assert picked.plugins[0].plugin == "second"

    plain = load_config(write(tmp_path, "plugins:\n  - name: a\n    plugin: m:C\n"), env={})
    assert plain.plugins[0].plugin == "m:C" and plain.plugins[0].source is None

    with pytest.raises(ConfigError, match=r"plugins\.0") as neither:
        load_config(write(tmp_path, "plugins:\n  - name: a\n"), env={})
    assert "plugin" in str(neither.value) and "source" in str(neither.value)

    with pytest.raises(ConfigError, match=r"plugins\.0\.plugin") as module_class:
        load_config(
            write(tmp_path, "plugins:\n  - name: a\n    source: ./pkg\n    plugin: m:C\n"),
            env={},
        )
    assert "entry point" in str(module_class.value)

    for bad in ("foo/bar", "git::https://h/r?depth=1", "./a//b", "git@github.com:o/r"):
        with pytest.raises(ConfigError) as expected:
            parse_source(bad)
        with pytest.raises(ConfigError, match=r"plugins\.0\.source") as loaded:
            load_config(
                write(tmp_path, f"plugins:\n  - name: a\n    source: '{bad}'\n"),
                env={},
            )
        assert str(expected.value) in str(loaded.value)


# ---------------------------------------------------------------- ac2


def _package(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True)
    (directory / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "1"\n')
    return directory


ENTRY_POINTS = {
    "any-mcp-source-plugin": {"source_plugin": "source_plugin:SourcePlugin"},
    "multi-plugin": {
        "first": "source_plugin:SourcePlugin",
        "second": "stdio_plugin:StdioPlugin",
    },
}


def _distribution(name: str) -> SimpleNamespace:
    eps = EntryPoints(EntryPoint(n, v, GROUP) for n, v in ENTRY_POINTS[name].items())
    return SimpleNamespace(entry_points=eps)


@pytest.fixture
def fake_install(monkeypatch) -> list[tuple[str, Path, bool]]:
    """Route ``any_mcp.install.install`` through the real installer with pip faked."""
    calls: list[tuple[str, Path, bool]] = []
    real = installer.install
    pips: list[list[str]] = []

    def run(argv, **kwargs):
        pips.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake(source, *, base_dir, refresh):
        calls.append((source.raw, base_dir, refresh))
        return real(source, base_dir=base_dir, refresh=refresh, run=run, distribution=_distribution)

    monkeypatch.setattr(installer, "install", fake)
    monkeypatch.syspath_prepend(str(FIXTURE / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "tests" / "fixtures"))
    return calls


def test_load_plugins_source_entries(tmp_path, fake_install):
    conf = tmp_path / "conf"
    _package(conf / "single", "any-mcp-source-plugin")
    _package(conf / "multi", "multi-plugin")
    path = write(
        conf,
        "plugins:\n"
        "  - name: one\n    source: ./single\n"
        "  - name: two\n    source: ./multi\n    plugin: second\n",
    )
    config = load_config(path, env={})
    loaded = load_plugins(config)
    assert [type(p.instance).__name__ for p in loaded] == ["SourcePlugin", "StdioPlugin"]
    assert [p.ctx.namespace for p in loaded] == ["one", "two"]
    assert fake_install == [("./single", conf.resolve(), False), ("./multi", conf.resolve(), False)]
    manifest = json.loads((tmp_path / "cache" / "manifest.json").read_text())
    assert len(manifest["entries"]) == 2

    # A manifest hit loads without installing again.
    again = load_plugins(load_config(path, env={}))
    assert [type(p.instance).__name__ for p in again] == ["SourcePlugin", "StdioPlugin"]
    assert len(fake_install) == 2

    ambiguous = write(conf, "plugins:\n  - name: two\n    source: ./multi\n", "ambiguous.yaml")
    with pytest.raises(ConfigError) as several:
        load_plugins(load_config(ambiguous, env={}))
    message = str(several.value)
    assert "./multi" in message and "first" in message and "second" in message

    unknown = write(
        conf, "plugins:\n  - name: two\n    source: ./multi\n    plugin: third\n", "unknown.yaml"
    )
    with pytest.raises(ConfigError) as missing:
        load_plugins(load_config(unknown, env={}))
    message = str(missing.value)
    assert "./multi" in message and "third" in message and "first" in message


# ---------------------------------------------------------------- ac3


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, Path, bool]]:
    calls: list[tuple[str, Path, bool]] = []

    def fake(source, *, base_dir, refresh):
        calls.append((source.raw, base_dir, refresh))
        local = source.kind == "local"
        return Installed(
            source=source,
            directory=base_dir,
            distribution=f"dist-{len(calls)}",
            sha=None if local else "a" * 40,
            entry_points={"x": "x:X"},
        )

    monkeypatch.setattr(installer, "install", fake)
    return calls


def test_plugins_install_command(tmp_path, monkeypatch, capsys, recorded):
    monkeypatch.chdir(tmp_path)
    git_source = "git::https://example.test/r.git//topic/p?ref=v1"
    assert main(["plugins", "install", "./here", git_source]) == 0
    assert recorded == [("./here", tmp_path, True), (git_source, tmp_path, True)]
    out, err = capsys.readouterr()
    assert out.splitlines() == ["./here dist-1 local", f"{git_source} dist-2 {'a' * 40}"]
    assert err == ""

    recorded.clear()
    conf = tmp_path / "conf"
    conf.mkdir()
    monkeypatch.setenv("SRC_ROOT", "/opt/plugins")
    monkeypatch.delenv("UNSET_TOKEN_XYZ", raising=False)
    path = write(
        conf,
        "server:\n  name: ${UNSET_NAME_XYZ}\n"
        "plugins:\n"
        "  - name: a\n    source: ${SRC_ROOT}/pkg\n    config:\n      token: ${UNSET_TOKEN_XYZ}\n"
        "  - name: b\n    plugin: some.mod:Cls\n"
        "  - name: c\n    source: ./rel\n",
    )
    monkeypatch.delenv("UNSET_NAME_XYZ", raising=False)
    assert main(["plugins", "install", "--config", str(path)]) == 0
    assert recorded == [("/opt/plugins/pkg", conf.resolve(), True), ("./rel", conf.resolve(), True)]
    out, err = capsys.readouterr()
    assert out.splitlines() == ["/opt/plugins/pkg dist-1 local", "./rel dist-2 local"]
    assert err == ""

    recorded.clear()
    unset = write(conf, "plugins:\n  - name: a\n    source: ${UNSET_TOKEN_XYZ}/pkg\n", "u.yaml")
    assert main(["plugins", "install", "--config", str(unset)]) == 78
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1
    assert "plugins.0.source" in err and "UNSET_TOKEN_XYZ" in err
    assert recorded == []


# ---------------------------------------------------------------- ac4


def test_install_failure_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["plugins", "install", "./does-not-exist"]) == 78
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1
    assert err.startswith("any-mcp: ") and "'./does-not-exist'" in err

    missing_repo = f"git::{(tmp_path / 'no-repo').as_uri()}//sub?ref=v1"
    assert main(["plugins", "install", missing_repo]) == 78
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1 and missing_repo in err

    assert main(["plugins", "install", "foo/bar"]) == 78
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1 and "'foo/bar'" in err

    config = write(tmp_path, "plugins:\n  - name: a\n    source: ./gone\n")
    assert main(["--config", str(config), "--list-tools"]) == 78
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1 and "'./gone'" in err


def test_load_failure_exit_code(tmp_path, monkeypatch, capsys, fake_install):
    _package(tmp_path / "multi", "multi-plugin")
    config = write(tmp_path, "plugins:\n  - name: a\n    source: ./multi\n")
    assert main(["--config", str(config), "--list-tools"]) == 78
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1 and "'./multi'" in err


# ---------------------------------------------------------------- ac5


def _run(cmd: list[str | Path], **kwargs) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True, timeout=600, **kwargs
    )
    assert proc.returncode == 0, f"{cmd} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc


def _tools(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [tool["name"] for tool in json.loads(proc.stdout)]


def test_end_to_end_local_and_git_sources(tmp_path):
    venv = tmp_path / "venv"
    _run([sys.executable, "-m", "venv", venv])
    python = venv / "bin" / "python"
    # Offline from uv's cache: any-mcp from the working tree, and hatchling, the fixture's build
    # backend with editables for its editable builds, which pip below uses without build
    # isolation because it may not reach an index.
    _run([UV, "pip", "install", "--offline", "--python", python, ROOT, "hatchling", "editables"])
    any_mcp = venv / "bin" / "any-mcp"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PIP_", "ANY_MCP_", "VIRTUAL_ENV", "PYTHON"))
    }
    # pip reads PIP_NO_BUILD_ISOLATION as the value of build_isolation: "0" turns it off.
    env |= {
        "ANY_MCP_CACHE_DIR": str(tmp_path / "cache"),
        "PIP_NO_INDEX": "1",
        "PIP_NO_BUILD_ISOLATION": "0",
    }
    ignore = shutil.ignore_patterns("__pycache__", "*.egg-info", "build", "dist")

    # Local directory source: installed (editable) on the first start, in the same process.
    shutil.copytree(FIXTURE, tmp_path / "plugins" / "source_plugin", ignore=ignore)
    local = write(tmp_path, "plugins:\n  - name: loc\n    source: ./plugins/source_plugin\n")
    listed = _run([any_mcp, "--config", local, "--list-tools"], env=env, cwd=tmp_path)
    assert _tools(listed) == ["loc_ping"]

    # git::file:// source with //subdir and ?ref=, pre-installed by the install command.
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "uploadpack.allowFilter", "true")
    shutil.copytree(FIXTURE, repo / "topic" / "source_plugin", ignore=ignore)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    git(repo, "tag", "v1")
    sha = git(repo, "rev-parse", "HEAD")
    source = f"git::{repo.as_uri()}//topic/source_plugin?ref=v1"
    installed = _run([any_mcp, "plugins", "install", source], env=env, cwd=tmp_path)
    assert installed.stdout.splitlines() == [f"{source} any-mcp-source-plugin {sha}"]
    remote = write(tmp_path, f"plugins:\n  - name: gt\n    source: '{source}'\n", "git.yaml")
    listed = _run([any_mcp, "--config", remote, "--list-tools"], env=env, cwd=tmp_path)
    assert _tools(listed) == ["gt_ping"]
