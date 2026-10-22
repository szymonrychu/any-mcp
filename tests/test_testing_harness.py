from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from any_mcp.errors import ConfigError
from any_mcp.plugin import Plugin
from any_mcp.testing import HarnessResult, PluginHarness
from any_mcp.types import ToolCall, ToolSpec

EVENTS: list[str] = []


class Echo(Plugin):
    async def setup(self) -> None:
        EVENTS.append("setup")

    async def teardown(self) -> None:
        EVENTS.append("teardown")

    async def load_tools(self) -> list[ToolSpec]:
        async def echo(call: ToolCall) -> str:
            return f"echo {call.arguments['text']}"

        async def boom(call: ToolCall) -> str:
            raise RuntimeError("nope")

        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        return [
            ToolSpec(name="echo", description="Echo", input_schema=schema, handler=echo),
            ToolSpec(name="boom", description="Boom", input_schema=schema, handler=boom),
        ]


class Cfg(BaseModel):
    base_url: str


class Fetch(Plugin):
    Config = Cfg

    async def load_tools(self) -> list[ToolSpec]:
        async def get(call: ToolCall) -> Any:
            response = await self.ctx.http.get("/thing")
            return response.json()

        spec = ToolSpec(name="get", description="Get", input_schema={"type": "object"}, handler=get)
        return [spec]


class Needy(Plugin):
    Config = Cfg

    async def load_tools(self) -> list[ToolSpec]:
        return []


class Changing(Plugin):
    names = ["one"]

    async def load_tools(self) -> list[ToolSpec]:
        async def h(call: ToolCall) -> str:
            return "x"

        return [
            ToolSpec(name=n, description=n, input_schema={"type": "object"}, handler=h)
            for n in type(self).names
        ]


async def test_harness_lists_tools() -> None:
    async with PluginHarness(Echo) as h:
        tools = await h.list_tools()
    assert {t["name"] for t in tools} == {"echo_echo", "echo_boom"} or {
        t["name"] for t in tools
    } == {"echo", "boom"}
    assert all("inputSchema" in t for t in tools)


async def test_harness_call() -> None:
    async with PluginHarness(Echo, namespace="ns") as h:
        names = {t["name"] for t in await h.list_tools()}
        exposed = next(n for n in names if n.endswith("echo"))
        local = await h.call("echo", {"text": "hi"})
        full = await h.call(exposed, {"text": "hi"})
        bad = await h.call("boom", {"text": "x"})
        invalid = await h.call("echo", {})
    assert isinstance(local, HarnessResult)
    assert local == full
    assert "echo hi" in local.text and local.is_error is False
    assert bad.is_error and invalid.is_error


async def test_harness_mock_transport() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    async with PluginHarness(
        Fetch, config={"base_url": "http://api.invalid"}, transport=httpx.MockTransport(handler)
    ) as h:
        result = await h.call("get")
    assert seen == ["http://api.invalid/thing"]
    assert result.structured == {"ok": True} or "true" in result.text.lower()


def test_harness_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = tmp_path / "plugins"
    pkg.mkdir()
    for mod, tool in (("harness_mod_a", "alpha"), ("harness_mod_b", "beta")):
        (pkg / f"{mod}.py").write_text(
            "from any_mcp.plugin import Plugin\nfrom any_mcp.types import ToolCall, ToolSpec\n"
            "class P(Plugin):\n"
            "    async def load_tools(self):\n"
            "        async def h(call):\n            return 'ok'\n"
            f"        return [ToolSpec(name='{tool}', description='d',\n"
            "                         input_schema={'type': 'object'}, handler=h)]\n"
        )
    (tmp_path / "any-mcp.yaml").write_text(
        "python_path: [plugins]\nplugins:\n"
        "  - {name: a, plugin: 'harness_mod_a:P'}\n"
        "  - {name: b, plugin: 'harness_mod_b:P'}\n"
    )
    monkeypatch.chdir(tmp_path / "plugins")
    monkeypatch.setattr(sys, "path", list(sys.path))

    import asyncio

    async def run() -> list[str]:
        async with PluginHarness.from_config(tmp_path / "any-mcp.yaml") as h:
            assert (await h.call("a_alpha")).text == "ok"
            return sorted(t["name"] for t in await h.list_tools())

    names = asyncio.run(run())
    assert names == ["a_alpha", "b_beta"]


async def test_harness_reload() -> None:
    Changing.names = ["one"]
    async with PluginHarness(Changing, name="c", namespace="c") as h:
        assert [t["name"] for t in await h.list_tools()] == ["c_one"]
        assert await h.reload() is False
        Changing.names = ["one", "two"]
        assert await h.reload() is True
        assert [t["name"] for t in await h.list_tools()] == ["c_one", "c_two"]
    Changing.names = ["one"]


async def test_harness_config_error() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        async with PluginHarness(Needy, config={}):
            pass


async def test_harness_teardown() -> None:
    EVENTS.clear()
    async with PluginHarness(Echo):
        assert EVENTS == ["setup"]
    assert EVENTS == ["setup", "teardown"]
