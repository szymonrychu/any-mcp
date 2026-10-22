"""The small and medium example plugins, run offline through the harness and the CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from any_mcp.config import load_config
from any_mcp.testing import PluginHarness

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
GITHUB_ISSUE = {
    "number": 7,
    "title": "Crash on start",
    "state": "open",
    "html_url": "https://github.com/o/r/issues/7",
    "body": "long text",
    "user": {"login": "ada"},
}


def example(name: str, module: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(EXAMPLES / name))
    sys.modules.pop(module, None)


async def test_hello_example(monkeypatch: pytest.MonkeyPatch) -> None:
    example("hello", "hello", monkeypatch)
    source = (EXAMPLES / "hello" / "hello.py").read_text()
    assert len(source.splitlines()) <= 15
    async with PluginHarness("hello:Hello") as harness:
        result = await harness.call("greet", {"name": "Ada"})
    assert not result.is_error
    assert "Hello, Ada!" in result.text


def test_hello_cli_list_tools() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "any_mcp",
            "--config",
            "examples/hello/any-mcp.yaml",
            "--list-tools",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert [t["name"] for t in json.loads(proc.stdout)] == ["hello_greet"]


async def test_single_endpoint_example(monkeypatch: pytest.MonkeyPatch) -> None:
    example("single_endpoint", "zippopotam", monkeypatch)
    source = (EXAMPLES / "single_endpoint" / "zippopotam.py").read_text()
    assert "async def call" not in source
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"post code": "90210", "places": []})

    async with PluginHarness("zippopotam:Zippopotam", transport=httpx.MockTransport(handler)) as h:
        tools = await h.list_tools()
        required = tools[0]["inputSchema"]["required"]
        result = await h.call("lookup_postal_code", {"country": "us", "postal_code": "90210"})
    assert sorted(required) == ["country", "postal_code"]
    assert not result.is_error, result.text
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url == httpx.URL("https://api.zippopotam.us/us/90210")


async def test_github_issues_example(monkeypatch: pytest.MonkeyPatch) -> None:
    example("github_issues", "github_issues", monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": 1, "body": "thanks"})
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[GITHUB_ISSUE])
        return httpx.Response(200, json=GITHUB_ISSUE)

    config = {"token": "tok", "base_url": "https://gh.test"}
    transport = httpx.MockTransport(handler)
    ref = "github_issues:GithubIssues"
    projected = {"number": 7, "title": "Crash on start", "state": "open"}
    projected["url"] = GITHUB_ISSUE["html_url"]

    async with PluginHarness(ref, config=config, transport=transport) as h:
        listed = await h.call("list_issues", {"repo": "o/r", "limit": 5})
        got = await h.call("get_issue", {"repo": "o/r", "number": 7})
        denied = await h.call("add_comment", {"repo": "o/r", "number": 7, "body": "hi"})
    assert json.loads(listed.text) == [projected]
    assert json.loads(got.text) == projected
    assert "body" not in listed.text and "ada" not in listed.text
    assert denied.is_error and "read_only" in denied.text
    assert [r.method for r in seen] == ["GET", "GET"]
    assert seen[0].url.params["per_page"] == "5" and seen[0].url.params["state"] == "open"
    assert seen[0].url.path == "/repos/o/r/issues"

    async with PluginHarness(ref, config={**config, "read_only": False}, transport=transport) as h:
        done = await h.call("add_comment", {"repo": "o/r", "number": 7, "body": "hi"})
    assert not done.is_error, done.text
    post = seen[-1]
    assert post.method == "POST" and post.url.path == "/repos/o/r/issues/7/comments"
    assert json.loads(post.content) == {"body": "hi"}
    for request in seen:
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_example_configs_load() -> None:
    env = {"GITHUB_TOKEN": "tok"}
    configs = sorted(EXAMPLES.glob("*/any-mcp.yaml"))
    assert {c.parent.name for c in configs} >= {"hello", "single_endpoint", "github_issues"}
    for path in configs:
        config = load_config(path, env=env)
        assert config.plugins, path
        assert all((path.parent / p).resolve() == Path(p) for p in config.python_path)
