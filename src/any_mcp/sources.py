"""Terraform-module-like plugin sources: the one parser of the source notation.

Accepted forms:

- local directory: ``./path``, ``../path`` or ``/path``, kept verbatim;
- git: ``git::<url>[//<subdir>][?ref=<ref>]`` with an ``https``, ``ssh`` or ``file`` URL;
- GitHub shorthand: ``github.com/<owner>/<repo>[//<subdir>][?ref=<ref>]``.

Pure string work: no I/O, no git call. Resolving and fetching is the installer's job.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from any_mcp.errors import ConfigError

_LOCAL_PREFIXES = ("./", "../", "/")
_GIT_PREFIX = "git::"
_GIT_SCHEMES = ("https", "ssh", "file")
_GITHUB_PREFIX = "github.com/"
_REF_CHARS = re.compile(r"[A-Za-z0-9._/-]+")
_SCP_LIKE = re.compile(r"[^/:@]+@[^/:]+:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BLANK_OR_CONTROL = re.compile(r"[\s\x00-\x1f\x7f]")
_SCP_HINT = "scp-like git addresses are not supported, use git::ssh://; "
_ACCEPTED = (
    "accepted forms: ./<path>, ../<path> or /<path> (local directory); "
    "git::https://<url>, git::ssh://<url> or git::file://<url> with optional //<subdir> "
    "and ?ref=<ref>; github.com/<owner>/<repo> with optional //<subdir> and ?ref=<ref>"
)


type _Fail = Callable[[str], ConfigError]


@dataclass(frozen=True)
class Source:
    """A parsed plugin source. ``path`` is set for local sources, ``url`` for git ones."""

    raw: str
    kind: Literal["local", "git"]
    path: str | None
    url: str | None
    subdir: str
    ref: str | None


def parse_source(text: str) -> Source:
    """Parse one plugin source; raise ConfigError naming the source and what is wrong."""

    def fail(reason: str) -> ConfigError:
        return ConfigError(f"plugin source '{text}': {reason}")

    remote = text.startswith((_GIT_PREFIX, _GITHUB_PREFIX))
    if remote and _BLANK_OR_CONTROL.search(text):
        raise fail("a remote source may not contain whitespace or control characters")
    if _CONTROL.search(text):
        raise fail("a source may not contain control characters")
    if text.startswith(_LOCAL_PREFIXES):
        if "//" in text or "?" in text:
            raise fail("a local directory path may not contain '//' or '?'")
        return Source(raw=text, kind="local", path=text, url=None, subdir="", ref=None)
    if text.startswith(_GIT_PREFIX):
        return _parse_git(text, text[len(_GIT_PREFIX) :], fail)
    if text.startswith(_GITHUB_PREFIX):
        return _parse_github(text, text[len(_GITHUB_PREFIX) :], fail)
    if _SCP_LIKE.match(text):
        raise fail(_SCP_HINT + _ACCEPTED)
    raise fail(f"unrecognised source; {_ACCEPTED}")


def _parse_git(text: str, rest: str, fail: _Fail) -> Source:
    scheme, separator, location = rest.partition("://")
    if not separator:
        hint = _SCP_HINT if _SCP_LIKE.match(rest) else ""
        raise fail(f"git:: needs a URL with a scheme; {hint}{_ACCEPTED}")
    if scheme not in _GIT_SCHEMES:
        raise fail(f"unsupported git URL scheme '{scheme}'; {_ACCEPTED}")
    location, subdir, ref = _split_tail(location, fail)
    if not location:
        raise fail("the git URL is empty")
    if scheme != "file":
        host, slash, path = location.partition("/")
        if not host or not slash or not path:
            raise fail("a git URL needs a non-empty host and a non-empty repository path")
    return Source(
        raw=text, kind="git", path=None, url=f"{scheme}://{location}", subdir=subdir, ref=ref
    )


def _parse_github(text: str, rest: str, fail: _Fail) -> Source:
    location, subdir, ref = _split_tail(rest, fail)
    owner, _, repo = location.partition("/")
    if not owner or not repo or "/" in repo:
        raise fail(f"expected github.com/<owner>/<repo>; {_ACCEPTED}")
    suffix = "" if repo.endswith(".git") else ".git"
    url = f"https://github.com/{owner}/{repo}{suffix}"
    return Source(raw=text, kind="git", path=None, url=url, subdir=subdir, ref=ref)


def _split_tail(location: str, fail: _Fail) -> tuple[str, str, str | None]:
    """Split ``<location>[//<subdir>][?<query>]`` into location, subdir and ref."""
    location, has_query, query = location.partition("?")
    ref = _parse_query(query, fail) if has_query else None
    location, has_subdir, subdir = location.partition("//")
    if has_subdir:
        _check_subdir(subdir, fail)
    return location, subdir, ref


def _parse_query(query: str, fail: _Fail) -> str:
    refs = []
    for item in query.split("&"):
        key, _, value = item.partition("=")
        if key != "ref":
            raise fail(f"unsupported query key '{key}', only 'ref' is allowed")
        refs.append(value)
    if len(refs) > 1:
        raise fail("ref given more than once")
    _check_ref(refs[0], fail)
    return refs[0]


def _check_ref(ref: str, fail: _Fail) -> None:
    if not ref:
        raise fail("empty ref")
    if ref.startswith("-"):
        raise fail(f"ref '{ref}' may not start with '-'")
    if not _REF_CHARS.fullmatch(ref):
        raise fail(f"ref '{ref}' may only contain A-Z, a-z, 0-9, '.', '_', '/' and '-'")
    if ".." in ref:
        raise fail(f"ref '{ref}' may not contain '..'")


def _check_subdir(subdir: str, fail: _Fail) -> None:
    if subdir.startswith("/"):
        raise fail(f"subdir '{subdir}' must be relative, without a leading '/'")
    segments = subdir.split("/")
    if "" in segments:
        raise fail(f"subdir '{subdir}' has an empty segment")
    if ".." in segments:
        raise fail(f"subdir '{subdir}' may not contain a '..' segment")
