# Testing a plugin

any-mcp ships a test harness, `any_mcp.testing.PluginHarness`. It builds the same config, loader, registry and server a deployment uses and talks to it with an in-process MCP client, so a test exercises validation, naming, hooks and response shaping exactly as a real client would, with no port opened and no network. It does not need pytest, but the snippets here are pytest tests (the project sets `asyncio_mode = "auto"`, so `async def test_...` just works).

## The harness

Serve a config file and call a tool. This test runs against the hello example from the repository root:

```python
from any_mcp.testing import PluginHarness


async def test_greet_through_the_harness():
    async with PluginHarness.from_config("examples/hello/any-mcp.yaml") as harness:
        tools = await harness.list_tools()
        result = await harness.call("greet", {"name": "Ada"})
    assert [tool["name"] for tool in tools] == ["hello_greet"]
    assert not result.is_error
    assert "Hello, Ada!" in result.text
```

- `PluginHarness(PluginClass, config=..., name=..., namespace=..., transport=..., tools=[...])` serves one plugin class (or a `"module:Class"` string). `PluginHarness.from_config(path)` serves every plugin of a config file; `python_path` resolves against the file.
- `async with` starts the plugins (`setup`, `load_tools`) and stops them (`teardown`).
- `await harness.list_tools()` returns the tools as a client sees them: MCP tool dicts with `name`, `description` and `inputSchema`.
- `await harness.call(tool, arguments)` returns a `HarnessResult` with `text`, `structured` and `is_error`. Pass the exposed name (`hello_greet`) or the plugin-local name (`greet`) when it is unambiguous. Invalid arguments and plugin errors come back as `is_error=True`, not as exceptions, just as they do for a client.
- `await harness.reload()` reloads every plugin and returns `True` when the tool list changed.

A plugin class defined in the test file works too, which is handy for a one-off:

```python
from any_mcp import Plugin, tool
from any_mcp.testing import PluginHarness


class Adder(Plugin):
    @tool
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


async def test_adder():
    async with PluginHarness(Adder, namespace="math") as harness:
        result = await harness.call("math_add", {"a": 2, "b": 3})
        invalid = await harness.call("math_add", {"a": "two", "b": 3})
    assert result.structured == 5 or "5" in result.text
    assert invalid.is_error
```

## Mocking HTTP

A plugin that calls an API sends its requests through `self.ctx.http`. Pass an `httpx.MockTransport` as `transport` and every request is routed to your handler instead of the network, so you can assert on exactly what the plugin sent. This test covers the single-endpoint example:

```python
import httpx

from any_mcp.testing import PluginHarness


async def test_lookup_sends_the_right_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"post code": "90210", "places": []})

    transport = httpx.MockTransport(handler)
    async with PluginHarness.from_config(
        "examples/single_endpoint/any-mcp.yaml", transport=transport
    ) as harness:
        result = await harness.call("lookup_postal_code", {"country": "us", "postal_code": "90210"})
    assert not result.is_error
    assert seen[0].method == "GET"
    assert seen[0].url == httpx.URL("https://api.zippopotam.us/us/90210")
```

Test the error paths the same way: return `httpx.Response(404)` and assert `result.is_error`, or a handler that raises `httpx.ConnectError` to simulate an outage. For a plugin with credentials, assert on `seen[0].headers["Authorization"]`.

## Testing a config

A plugin's config model is part of its contract, so test that a good config starts and a bad one fails clearly. A config that does not validate raises `ConfigError` when the harness is entered, naming the field:

```python
import httpx
import pytest

from any_mcp.errors import ConfigError
from any_mcp.testing import PluginHarness


async def test_github_requires_a_token():
    with pytest.raises(ConfigError, match="token"):
        async with PluginHarness("github_issues:GithubIssues", config={}):
            pass


async def test_read_only_blocks_comments():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    config = {"token": "t", "base_url": "https://gh.test", "read_only": True}
    async with PluginHarness(
        "github_issues:GithubIssues", config=config, transport=transport
    ) as harness:
        result = await harness.call("add_comment", {"repo": "o/r", "number": 1, "body": "hi"})
    assert result.is_error
    assert "read_only" in result.text
```

(Those two tests need `examples/github_issues` on `sys.path`; with pytest add `pythonpath = ["examples/github_issues"]` to `[tool.pytest.ini_options]`, or use `from_config` with a config file that sets `python_path`.)

To test a whole deployment config, point `PluginHarness.from_config` at it: every plugin in the file is loaded, so a typo in a plugin reference, a missing `python_path` entry or an invalid `config:` block fails the test instead of the deployment. Use `harness.list_tools()` to assert the exposed tool names, including namespaces. Use `PluginHarness(..., tools=["greet"])` to check an `allow` filter.
