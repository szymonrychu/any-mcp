"""Install plugin sources: fetch git sources into a cache, pip-install them into this environment.

A git source is fetched into ``cache_dir()`` (sparse, blob-filtered) and installed with
``<sys.executable> -m pip install``; a local source is installed editable in place.
``manifest.json`` in the cache records each installed source, so a later start reuses it without
git or pip. A SHA
ref is never fetched again; a branch, tag or default-branch source is re-resolved only by an
explicit install (``refresh=True``), and reinstalled only when its commit moved.

Every subprocess is an argv list run without a shell and with a timeout; URLs and paths given to
git follow ``--``, and refs (validated by the parser) follow ``--end-of-options`` or are attached
to their option with ``=``.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from any_mcp.errors import ConfigError
from any_mcp.sources import Source

GIT_TIMEOUT = 300
PIP_TIMEOUT = 900
CACHE_ENV = "ANY_MCP_CACHE_DIR"
MANIFEST = "manifest.json"
ENTRY_POINT_GROUP = "any_mcp.plugins"

_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
_TAIL = 500

type Runner = Callable[..., subprocess.CompletedProcess[str]]
type Lookup = Callable[[str], Distribution]
type RefKind = Literal["local", "sha", "commit", "branch", "tag", "default"]

# Ref kinds whose commit never moves: cached forever, even on an explicit install.
_FIXED = ("sha", "commit")


@dataclass(frozen=True)
class Installed:
    """An installed plugin source: its package directory, distribution and entry points."""

    source: Source
    directory: Path
    distribution: str
    sha: str | None
    entry_points: dict[str, str]


def cache_dir() -> Path:
    """``$ANY_MCP_CACHE_DIR``, else ``<sys.prefix>/any-mcp-sources``."""
    configured = os.environ.get(CACHE_ENV)
    return Path(configured) if configured else Path(sys.prefix) / "any-mcp-sources"


def installed(source: Source, *, base_dir: Path) -> Installed | None:
    """The manifest entry of ``source``, without any git or pip call; None when not installed."""
    entry = _read_manifest(cache_dir()).get(_key(source, base_dir))
    return _from_entry(source, entry) if entry else None


def install(
    source: Source,
    *,
    base_dir: Path,
    refresh: bool,
    run: Runner = subprocess.run,
    distribution: Lookup = importlib.metadata.distribution,
) -> Installed:
    """Fetch ``source`` when needed, pip-install it and read its entry points.

    ``refresh=True`` is an explicit install: branch, tag and default-branch sources are
    re-resolved and reinstalled when their commit moved, local sources are reinstalled.
    ``run`` and ``distribution`` are injectable for tests.
    """
    root = cache_dir()
    key = _key(source, base_dir)
    manifest = _read_manifest(root)
    entry = manifest.get(key)
    if entry and not refresh:
        return _from_entry(source, entry)
    job = _Job(source, run, distribution)
    if source.kind == "local":
        new_entry = job.install_local(_local_dir(source, base_dir))
    else:
        if entry and entry["kind"] in _FIXED:
            return _from_entry(source, entry)
        new_entry = job.install_git(root, key, entry)
        if new_entry is entry:
            return _from_entry(source, entry)
    manifest[key] = new_entry
    _write_manifest(root, manifest)
    old_checkout = entry.get("checkout") if entry else None
    if old_checkout and old_checkout != new_entry["checkout"]:
        shutil.rmtree(old_checkout, ignore_errors=True)
    return _from_entry(source, new_entry)


class _Job:
    """One install of one source; every error names the source."""

    def __init__(self, source: Source, run: Runner, distribution: Lookup) -> None:
        self.source = source
        self.run = run
        self.distribution = distribution

    def fail(self, reason: str) -> ConfigError:
        return ConfigError(f"plugin source '{self.source.raw}': {reason}")

    def install_local(self, directory: Path) -> dict[str, Any]:
        if not directory.is_dir():
            raise self.fail(f"directory {directory} does not exist")
        name, entry_points = self.install_package(directory, editable=True)
        return _entry(self.source, "local", None, None, directory, name, entry_points)

    def install_git(self, root: Path, key: str, entry: dict[str, Any] | None) -> dict[str, Any]:
        """Fetch and install; return ``entry`` itself when its commit has not moved."""
        kind, target = self.resolve()
        if entry and target is not None and entry["sha"] == target:
            return entry
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        partial = Path(tempfile.mkdtemp(prefix=".partial-", dir=src))
        checkout: Path | None = None
        try:
            sha = self.fetch(kind, partial)
            self.check_subdir(partial, sha)
            checkout = src / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}-{sha[:12]}"
            shutil.rmtree(checkout, ignore_errors=True)
            partial.rename(checkout)
            directory = checkout / self.source.subdir if self.source.subdir else checkout
            name, entry_points = self.install_package(directory, editable=False)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            if checkout is not None:
                shutil.rmtree(checkout, ignore_errors=True)
            raise
        return _entry(self.source, kind, sha, checkout, directory, name, entry_points)

    def resolve(self) -> tuple[RefKind, str | None]:
        """The ref kind and, unless it is a short SHA, the commit it names on the remote."""
        ref = self.source.ref
        if ref is not None and _FULL_SHA.fullmatch(ref):
            return "sha", ref.lower()
        done = self.git(["ls-remote", "--", self.url], what="ls-remote")
        refs: dict[str, str] = {}
        for line in done.stdout.splitlines():
            sha, _, name = line.partition("\t")
            refs[name] = sha
        if ref is None:
            if "HEAD" not in refs:
                raise self.fail(f"{self.url} has no default branch (no HEAD)")
            return "default", refs["HEAD"]
        if f"refs/heads/{ref}" in refs:
            return "branch", refs[f"refs/heads/{ref}"]
        tag = f"refs/tags/{ref}"
        if tag in refs:
            return "tag", refs.get(f"{tag}^{{}}", refs[tag])
        return "commit", None

    def fetch(self, kind: RefKind, path: Path) -> str:
        """Check the source out into the empty directory ``path``; return the commit SHA."""
        ref = self.source.ref
        subdir = self.source.subdir
        sparse = ["--sparse"] if subdir else []
        if kind == "sha":
            assert ref is not None
            self.git(["init", "--quiet", "--", str(path)], what="init")
            self.git(["remote", "add", "--", "origin", self.url], cwd=path, what="remote add")
            self.sparse_checkout(path)
            shallow = ["fetch", "--quiet", "--depth", "1", "--filter=blob:none"]
            if self.git([*shallow, "--", "origin", ref.lower()], cwd=path, check=False).returncode:
                full = ["fetch", "--quiet", "--tags", "--filter=blob:none", "--", "origin"]
                self.git(full, cwd=path, what="fetch")
            self.checkout(path, ref.lower())
        elif kind == "commit":
            assert ref is not None
            clone = ["clone", "--quiet", "--filter=blob:none", "--no-checkout", *sparse]
            self.git([*clone, "--", self.url, str(path)], what="clone")
            self.sparse_checkout(path)
            self.checkout(path, ref)
        else:
            branch = [f"--branch={ref}"] if ref is not None else []
            clone = ["clone", "--quiet", "--depth", "1", "--filter=blob:none", *sparse, *branch]
            self.git([*clone, "--", self.url, str(path)], what="clone")
            self.sparse_checkout(path)
        return self.git(["rev-parse", "HEAD"], cwd=path, what="rev-parse").stdout.strip()

    def sparse_checkout(self, path: Path) -> None:
        if self.source.subdir:
            argv = ["sparse-checkout", "set", "--", self.source.subdir]
            self.git(argv, cwd=path, what="sparse-checkout")

    def checkout(self, path: Path, ref: str) -> None:
        argv = ["checkout", "--quiet", "--detach", "--end-of-options", ref]
        done = self.git(argv, cwd=path, check=False)
        if done.returncode:
            raise self.fail(
                f"ref '{ref}' is not a branch, tag or commit of {self.url}: {_tail(done.stderr)}"
            )

    def check_subdir(self, checkout: Path, sha: str) -> None:
        subdir = self.source.subdir
        if not subdir:
            return
        resolved = (checkout / subdir).resolve()
        if not resolved.is_relative_to(checkout.resolve()):
            raise self.fail(f"subdir '{subdir}' resolves outside the checkout of {self.url}")
        if not resolved.is_dir():
            ref = self.source.ref or "the default branch"
            raise self.fail(
                f"subdir '{subdir}' does not exist in {self.url} at ref {ref} (commit {sha})"
            )

    def install_package(self, directory: Path, *, editable: bool) -> tuple[str, dict[str, str]]:
        """Pip-install ``directory``; return the distribution name and its plugin entry points."""
        name = self.project_name(directory)
        argv = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
        argv += ["-e", str(directory)] if editable else [str(directory)]
        done = self.call(argv, timeout=PIP_TIMEOUT)
        if done.returncode:
            if "No module named pip" in done.stderr:
                raise self.fail(
                    f"pip is not installed in the environment of {sys.executable}; "
                    "plugin sources need it"
                )
            raise self.fail(f"pip install failed (exit {done.returncode}): {_tail(done.stderr)}")
        importlib.invalidate_caches()
        try:
            found = self.distribution(name)
        except PackageNotFoundError:
            raise self.fail(
                f"distribution '{name}' is not installed after pip install of {directory}"
            ) from None
        entry_points = {
            ep.name: ep.value for ep in found.entry_points.select(group=ENTRY_POINT_GROUP)
        }
        if not entry_points:
            raise self.fail(f"distribution '{name}' declares no '{ENTRY_POINT_GROUP}' entry point")
        return name, entry_points

    def project_name(self, directory: Path) -> str:
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            raise self.fail(f"{directory} has no pyproject.toml")
        try:
            data = tomllib.loads(pyproject.read_text())
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise self.fail(f"cannot read {pyproject}: {exc}") from exc
        project = data.get("project")
        name = project.get("name") if isinstance(project, dict) else None
        if not isinstance(name, str) or not name:
            raise self.fail(f"{pyproject} does not declare [project].name")
        return name

    @property
    def url(self) -> str:
        assert self.source.url is not None
        return self.source.url

    def git(
        self, args: list[str], *, cwd: Path | None = None, what: str = "", check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        done = self.call(["git", *args], timeout=GIT_TIMEOUT, cwd=cwd, env=env)
        if check and done.returncode:
            raise self.fail(
                f"git {what} failed for {self.url} (exit {done.returncode}): {_tail(done.stderr)}"
            )
        return done

    def call(
        self,
        argv: list[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self.run(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise self.fail(f"{' '.join(argv[:2])} timed out after {timeout:g}s") from None
        except OSError as exc:
            raise self.fail(f"cannot run {argv[0]}: {exc}") from exc


def _tail(text: str) -> str:
    return " ".join(text.split())[-_TAIL:]


def _local_dir(source: Source, base_dir: Path) -> Path:
    assert source.path is not None
    path = Path(source.path)
    return (path if path.is_absolute() else base_dir / path).resolve()


def _normalise_url(url: str) -> str:
    parts = urlsplit(url)
    user, at, host = parts.netloc.rpartition("@")
    path = parts.path.rstrip("/").removesuffix(".git")
    return urlunsplit((parts.scheme.lower(), f"{user}{at}{host.lower()}", path, "", ""))


def _key(source: Source, base_dir: Path) -> str:
    if source.kind == "local":
        return json.dumps(["local", str(_local_dir(source, base_dir))])
    assert source.url is not None
    return json.dumps(["git", _normalise_url(source.url), source.ref, source.subdir])


def _entry(
    source: Source,
    kind: RefKind,
    sha: str | None,
    checkout: Path | None,
    directory: Path,
    distribution: str,
    entry_points: dict[str, str],
) -> dict[str, Any]:
    return {
        "source": source.raw,
        "kind": kind,
        "sha": sha,
        "checkout": str(checkout) if checkout else None,
        "directory": str(directory),
        "distribution": distribution,
        "entry_points": entry_points,
    }


def _from_entry(source: Source, entry: dict[str, Any]) -> Installed:
    return Installed(
        source=source,
        directory=Path(entry["directory"]),
        distribution=entry["distribution"],
        sha=entry["sha"],
        entry_points=dict(entry["entry_points"]),
    )


def _read_manifest(root: Path) -> dict[str, dict[str, Any]]:
    path = root / MANIFEST
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"plugin source manifest {path}: cannot read: {exc}") from exc
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise ConfigError(f"plugin source manifest {path}: no 'entries' mapping")
    return entries


def _write_manifest(root: Path, entries: dict[str, dict[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=root)
    with os.fdopen(fd, "w") as handle:
        json.dump({"entries": entries}, handle, indent=2, sort_keys=True)
    os.replace(temp, root / MANIFEST)
