"""Installer tests: git runs for real against bare repositories in tmp_path (file:// URLs).

pip and the entry-point lookup are faked through the injectable runner and lookup, so nothing
is installed into the dev environment and nothing reaches the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, EntryPoints, PackageNotFoundError
from pathlib import Path

import pytest

from any_mcp.errors import ConfigError
from any_mcp.install import Installed, cache_dir, install, installed
from any_mcp.sources import parse_source

PIP = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
GROUP = "any_mcp.plugins"
PYPROJECT = '[project]\nname = "{name}"\nversion = "{version}"\n'


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout.strip()


@dataclass
class Remote:
    """A bare repository with a plugin package under ``plugins/demo``."""

    work: Path
    bare: Path
    shas: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def commit(self, branch: str, message: str, files: dict[str, str]) -> str:
        git(self.work, "checkout", "-q", branch)
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        git(self.work, "push", "-q", "origin", branch)
        return git(self.work, "rev-parse", "HEAD")

    def move_tag(self, tag: str, sha: str) -> None:
        git(self.work, "tag", "-f", "-a", "-m", tag, tag, sha)
        git(self.work, "push", "-q", "-f", "origin", f"refs/tags/{tag}")


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "cache"
    monkeypatch.setenv("ANY_MCP_CACHE_DIR", str(path))
    return path


@pytest.fixture
def remote(tmp_path) -> Remote:
    work = tmp_path / "work"
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(bare, "config", "uploadpack.allowFilter", "true")
    git(tmp_path, "init", "-q", str(work))
    git(work, "remote", "add", "origin", str(bare))
    git(work, "commit", "-q", "--allow-empty", "-m", "root")
    git(work, "push", "-q", "origin", "main")
    repo = Remote(work=work, bare=bare)
    repo.shas["one"] = repo.commit(
        "main",
        "one",
        {
            "README.md": "root file\n",
            "plugins/demo/pyproject.toml": PYPROJECT.format(name="demo-plugin", version="0.1"),
            "plugins/demo/demo_plugin/__init__.py": "VERSION = 1\n",
            "other/data.txt": "not in the sparse checkout\n",
        },
    )
    repo.move_tag("v1", repo.shas["one"])
    git(work, "checkout", "-q", "-b", "dev")
    repo.shas["two"] = repo.commit(
        "dev",
        "two",
        {
            "plugins/demo/pyproject.toml": PYPROJECT.format(name="demo-plugin", version="0.2"),
            "plugins/demo/demo_plugin/__init__.py": "VERSION = 2\n",
        },
    )
    outside = tmp_path / "outside" / "pkg"
    outside.mkdir(parents=True)
    (outside / "pyproject.toml").write_text(PYPROJECT.format(name="evil", version="1"))
    git(work, "checkout", "-q", "-b", "evil", "main")
    (work / "escape").symlink_to(tmp_path / "outside")
    repo.shas["evil"] = repo.commit("evil", "evil", {})
    git(work, "checkout", "-q", "main")
    return repo


class Runner:
    """Records every subprocess call; git runs for real, pip succeeds without running."""

    def __init__(
        self,
        *,
        fail: Callable[[list[str]], bool] = lambda argv: False,
        pip: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.fail = fail
        self.pip = pip

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.fail(argv):
            return subprocess.CompletedProcess(argv, 128, "", "fatal: injected failure")
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        assert argv[:4] == [sys.executable, "-m", "pip", "install"], argv
        return self.pip or subprocess.CompletedProcess(argv, 0, "Successfully installed", "")

    @property
    def git(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[0] == "git"]

    @property
    def pips(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[0] != "git"]

    def git_commands(self) -> list[str]:
        """The git subcommand of each git call (the first argument that is not an option)."""
        commands = []
        for argv in self.git:
            args = iter(argv[1:])
            for arg in args:
                if arg == "-c":
                    next(args)
                elif not arg.startswith("-"):
                    commands.append(arg)
                    break
        return commands


class FakeDist:
    def __init__(self, entry_points: dict[str, str], group: str = GROUP) -> None:
        self.entry_points = EntryPoints(
            EntryPoint(name, value, group) for name, value in entry_points.items()
        )


def lookup(entry_points: dict[str, str] | None = None, group: str = GROUP):
    eps = {"demo": "demo_plugin:Demo"} if entry_points is None else entry_points

    def distribution(name: str) -> FakeDist:
        if name not in ("demo-plugin", "local-plugin"):
            raise PackageNotFoundError(name)
        return FakeDist(eps, group)

    return distribution


def run_install(
    text: str,
    runner: Runner,
    *,
    base_dir: Path | None = None,
    refresh: bool = False,
    distribution=None,
) -> Installed:
    return install(
        parse_source(text),
        base_dir=base_dir or Path.cwd(),
        refresh=refresh,
        run=runner,
        distribution=distribution or lookup(),
    )


def manifest(cache: Path) -> dict:
    return json.loads((cache / "manifest.json").read_text())


def entry_for(cache: Path, text: str) -> dict:
    (entry,) = [e for e in manifest(cache)["entries"].values() if e["source"] == text]
    return entry


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


def checkout_of(result: Installed, subdir: str) -> Path:
    path = result.directory
    for _ in subdir.split("/"):
        path = path.parent
    return path


def test_git_tag_and_branch_install(remote, cache):
    for ref, sha in (("v1", remote.shas["one"]), ("dev", remote.shas["two"])):
        text = f"git::{remote.url}//plugins/demo?ref={ref}"
        runner = Runner()
        result = run_install(text, runner)
        checkout = checkout_of(result, "plugins/demo")

        assert result.sha == sha
        assert result.distribution == "demo-plugin"
        assert result.entry_points == {"demo": "demo_plugin:Demo"}
        assert result.source == parse_source(text)
        assert head(checkout) == sha
        assert (result.directory / "pyproject.toml").is_file()
        assert not (checkout / "other").exists()
        assert git(checkout, "rev-parse", "--is-shallow-repository") == "true"
        assert git(checkout, "config", "remote.origin.partialclonefilter") == "blob:none"
        assert checkout.is_relative_to(cache)

        (clone,) = [argv for argv in runner.git if "clone" in argv]
        assert {"--depth", "1", "--filter=blob:none", f"--branch={ref}"} <= set(clone)
        assert "ls-remote" in runner.git_commands()
        assert runner.pips == [[*PIP, str(result.directory)]]

        entry = entry_for(cache, text)
        assert entry["sha"] == sha
        assert entry["directory"] == str(result.directory)
        assert entry["checkout"] == str(checkout)
        assert entry["distribution"] == "demo-plugin"
        assert installed(parse_source(text), base_dir=Path.cwd()) == result


def test_sha_refs_and_fallback(remote, cache, monkeypatch, tmp_path):
    full = remote.shas["two"]

    runner = Runner()
    result = run_install(f"git::{remote.url}//plugins/demo?ref={full}", runner)
    assert result.sha == full == head(checkout_of(result, "plugins/demo"))
    (fetch,) = [argv for argv in runner.git if "fetch" in argv]
    assert fetch[-3:] == ["--", "origin", full] and "--depth" in fetch
    assert "clone" not in runner.git_commands()

    monkeypatch.setenv("ANY_MCP_CACHE_DIR", str(tmp_path / "cache-fallback"))
    runner = Runner(fail=lambda argv: "fetch" in argv and "--depth" in argv)
    result = run_install(f"git::{remote.url}//plugins/demo?ref={full}", runner)
    assert result.sha == full == head(checkout_of(result, "plugins/demo"))
    fetches = [argv for argv in runner.git if "fetch" in argv]
    assert len(fetches) == 2
    assert "--depth" not in fetches[1] and "--filter=blob:none" in fetches[1]
    assert runner.git_commands()[-2:] == ["checkout", "rev-parse"]
    assert full in runner.git[-2]

    short = remote.shas["one"][:10]
    runner = Runner()
    result = run_install(f"git::{remote.url}//plugins/demo?ref={short}", runner)
    assert result.sha == remote.shas["one"] == head(checkout_of(result, "plugins/demo"))
    (clone,) = [argv for argv in runner.git if "clone" in argv]
    assert "--filter=blob:none" in clone and "--depth" not in clone
    assert [argv for argv in runner.git if "checkout" in argv and short in argv]

    runner = Runner()
    result = run_install(f"git::{remote.url}//plugins/demo", runner)
    assert result.sha == remote.shas["one"] == head(checkout_of(result, "plugins/demo"))
    (clone,) = [argv for argv in runner.git if "clone" in argv]
    assert "--depth" in clone and not [arg for arg in clone if arg.startswith("--branch")]
    assert entry_for(cache_dir(), f"git::{remote.url}//plugins/demo")["sha"] == remote.shas["one"]


def test_cache_rules(remote, cache):
    sha_text = f"git::{remote.url}//plugins/demo?ref={remote.shas['two']}"
    first = run_install(sha_text, Runner())
    for refresh in (False, True):
        runner = Runner()
        assert run_install(sha_text, runner, refresh=refresh) == first
        assert runner.calls == []

    for ref in ("dev", "v1"):
        text = f"git::{remote.url}//plugins/demo?ref={ref}"
        first = run_install(text, Runner())
        old_checkout = checkout_of(first, "plugins/demo")

        runner = Runner()
        assert run_install(text, runner) == first
        assert runner.calls == []

        runner = Runner()
        assert run_install(text, runner, refresh=True) == first
        assert runner.git_commands() == ["ls-remote"] and runner.pips == []

        moved = remote.commit("dev", f"moved {ref}", {"plugins/demo/demo_plugin/x.py": ref})
        if ref == "v1":
            remote.move_tag("v1", moved)

        runner = Runner()
        assert run_install(text, runner) == first
        assert runner.calls == []

        runner = Runner()
        again = run_install(text, runner, refresh=True)
        assert again.sha == moved == head(checkout_of(again, "plugins/demo"))
        assert runner.pips == [[*PIP, str(again.directory)]]
        assert entry_for(cache, text)["sha"] == moved
        assert installed(parse_source(text), base_dir=Path.cwd()) == again
        assert not old_checkout.exists()


def test_local_source_editable_in_place(tmp_path, cache):
    base = tmp_path / "config"
    package = base / "plugins" / "local"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text(PYPROJECT.format(name="local-plugin", version="1"))

    runner = Runner()
    result = run_install("./plugins/local", runner, base_dir=base)
    assert result.directory == package.resolve()
    assert result.sha is None
    assert result.distribution == "local-plugin"
    assert runner.git == []
    assert runner.pips == [[*PIP, "-e", str(package.resolve())]]
    assert sorted(path.name for path in cache.iterdir()) == ["manifest.json"]
    assert entry_for(cache, "./plugins/local")["directory"] == str(package.resolve())
    assert installed(parse_source("./plugins/local"), base_dir=base) == result
    assert installed(parse_source("./plugins/local"), base_dir=tmp_path) is None

    runner = Runner()
    assert run_install("./plugins/local", runner, base_dir=base) == result
    assert runner.calls == []

    runner = Runner()
    absolute = run_install(str(package), runner, base_dir=tmp_path / "elsewhere")
    assert absolute.directory == package.resolve()
    assert runner.calls == []

    runner = Runner()
    assert run_install("./plugins/local", runner, base_dir=base, refresh=True) == result
    assert runner.pips == [[*PIP, "-e", str(package.resolve())]]


def test_subdir_missing_or_escaping(remote, cache):
    text = f"git::{remote.url}//plugins/nope?ref=v1"
    runner = Runner()
    with pytest.raises(ConfigError) as info:
        run_install(text, runner)
    message = str(info.value)
    for part in (remote.url, "v1", remote.shas["one"], "plugins/nope"):
        assert part in message
    assert runner.pips == []

    text = f"git::{remote.url}//escape/pkg?ref=evil"
    runner = Runner()
    with pytest.raises(ConfigError, match="outside the checkout") as info:
        run_install(text, runner)
    assert "escape/pkg" in str(info.value)
    assert runner.pips == []
    assert not (cache / "manifest.json").exists()
    assert list((cache / "src").iterdir()) == []


def test_package_errors(tmp_path, cache):
    def refused(path: Path, runner: Runner, distribution=None) -> str:
        text = str(path)
        with pytest.raises(ConfigError) as info:
            run_install(text, runner, distribution=distribution)
        message = str(info.value)
        assert message.startswith(f"plugin source '{text}': ")
        assert not (cache / "manifest.json").exists()
        return message

    missing = tmp_path / "missing"
    missing.mkdir()
    assert "pyproject.toml" in refused(missing, Runner())

    for name, body, expected in (
        ("noname", '[project]\nversion = "1"\n', "[project].name"),
        ("noproject", '[tool.x]\nname = "x"\n', "[project].name"),
        ("broken", "[project\n", "cannot read"),
    ):
        package = tmp_path / name
        package.mkdir()
        (package / "pyproject.toml").write_text(body)
        runner = Runner()
        message = refused(package, runner)
        assert expected in message and "pyproject.toml" in message
        assert runner.pips == []

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "pyproject.toml").write_text(PYPROJECT.format(name="local-plugin", version="1"))
    assert "no 'any_mcp.plugins' entry point" in refused(package, Runner(), lookup({}))
    assert "no 'any_mcp.plugins' entry point" in refused(
        package, Runner(), lookup({"x": "m:X"}, group="console_scripts")
    )

    failed = subprocess.CompletedProcess([], 1, "", "ERROR: boom happened\n")
    message = refused(package, Runner(pip=failed))
    assert "pip install failed" in message and "boom happened" in message

    no_pip = subprocess.CompletedProcess([], 1, "", f"{sys.executable}: No module named pip\n")
    assert "pip is not installed" in refused(package, Runner(pip=no_pip))

    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text(PYPROJECT.format(name="not-there", version="1"))
    assert "not-there" in refused(other, Runner())


def test_git_failures_are_config_errors(remote, tmp_path):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    text = f"git::{remote.url}//plugins/demo?ref=dev"
    with pytest.raises(ConfigError, match="timed out") as info:
        install(parse_source(text), base_dir=tmp_path, refresh=False, run=timeout)
    assert str(info.value).startswith(f"plugin source '{text}': ")

    gone = (tmp_path / "gone.git").as_uri()
    with pytest.raises(ConfigError, match="git ls-remote failed for") as info:
        run_install(f"git::{gone}?ref=main", Runner())
    assert gone in str(info.value)

    with pytest.raises(ConfigError, match="nope-ref"):
        run_install(f"git::{remote.url}//plugins/demo?ref=nope-ref", Runner())


def test_subprocess_hygiene(remote, cache, monkeypatch, tmp_path):
    runner = Runner()
    sources = [
        f"git::{remote.url}//plugins/demo?ref=v1",
        f"git::{remote.url}//plugins/demo?ref=dev",
        f"git::{remote.url}//plugins/demo?ref={remote.shas['one']}",
        f"git::{remote.url}//plugins/demo?ref={remote.shas['two'][:9]}",
        f"git::{remote.url}//plugins/demo",
    ]
    for text in sources:
        run_install(text, runner)
    run_install(sources[1], runner, refresh=True)
    monkeypatch.setenv("ANY_MCP_CACHE_DIR", str(tmp_path / "cache-fallback"))
    fallback = Runner(fail=lambda argv: "fetch" in argv and "--depth" in argv)
    run_install(sources[2], fallback)
    package = tmp_path / "local"
    package.mkdir()
    (package / "pyproject.toml").write_text(PYPROJECT.format(name="local-plugin", version="1"))
    run_install(str(package), runner)

    calls = runner.calls + fallback.calls
    assert {"ls-remote", "clone", "init", "fetch", "sparse-checkout", "checkout"} <= set(
        runner.git_commands() + fallback.git_commands()
    )
    assert len([argv for argv, _ in calls if argv[0] != "git"]) == 7
    for argv, kwargs in calls:
        assert isinstance(argv, list) and all(isinstance(arg, str) for arg in argv)
        assert not kwargs.get("shell", False)
        assert isinstance(kwargs["timeout"], int | float) and kwargs["timeout"] > 0
        if argv[0] != "git":
            continue
        assert "-C" not in argv
        user_values = [
            index
            for index, arg in enumerate(argv)
            if arg == remote.url or os.path.isabs(arg) or arg == "plugins/demo"
        ]
        if user_values:
            assert "--" in argv, argv
            assert min(user_values) > argv.index("--"), argv


def test_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ANY_MCP_CACHE_DIR", str(tmp_path / "custom"))
    assert cache_dir() == tmp_path / "custom"

    monkeypatch.delenv("ANY_MCP_CACHE_DIR")
    assert cache_dir() == Path(sys.prefix) / "any-mcp-sources"

    monkeypatch.setenv("ANY_MCP_CACHE_DIR", str(tmp_path / "custom"))
    package = tmp_path / "local"
    package.mkdir()
    (package / "pyproject.toml").write_text(PYPROJECT.format(name="local-plugin", version="1"))
    run_install(str(package), Runner())
    assert (tmp_path / "custom" / "manifest.json").is_file()
