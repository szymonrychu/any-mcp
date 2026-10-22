from __future__ import annotations

import asyncio
import json
import logging
import sys
import textwrap
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from any_mcp._fastmcp_adapter import (
    InMemoryClient,
    ProtocolError,
    Server,
    build_server,
    in_memory_client,
)
from any_mcp.config import AnyMcpConfig, PluginEntry
from any_mcp.context import NoConfig, PluginContext
from any_mcp.discovery import LoadedPlugin
from any_mcp.plugin import Plugin
from any_mcp.registry import ToolRegistry
from any_mcp.types import ToolCall, ToolSpec

TOOL_CHANGED = "notifications/tools/list_changed"

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
    "additionalProperties": False,
}
ECHO_OUTPUT = {"type": "object", "properties": {"echo": {"type": "integer"}}}


async def _echo(call: ToolCall) -> Any:
    return {"echo": call.arguments["x"]}


async def _boom(call: ToolCall) -> Any:
    raise RuntimeError("boom secret-detail")


async def _pair(call: ToolCall) -> Any:
    return [1, 2]


async def _added(call: ToolCall) -> Any:
    return {"added": True}


def _base_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="echo",
            description="Echo x back.",
            input_schema=ECHO_SCHEMA,
            output_schema=ECHO_OUTPUT,
            title="Echo",
            annotations={"readOnlyHint": True, "openWorldHint": False},
            handler=_echo,
        ),
        ToolSpec(name="boom", description="Always fails.", handler=_boom),
        ToolSpec(name="pair", description="Returns a JSON array.", handler=_pair),
    ]


class Shop(Plugin):
    """A plugin whose tool list a test can change between reloads."""

    def __init__(self) -> None:
        self.specs: list[ToolSpec] = _base_specs()
        self.events: list[str] = []

    async def setup(self) -> None:
        self.events.append("setup")

    async def teardown(self) -> None:
        self.events.append("teardown")

    async def load_tools(self) -> Sequence[ToolSpec]:
        self.events.append("load_tools")
        return list(self.specs)


def _server(
    plugin: Shop | None = None,
    *,
    reload_every: float | None = None,
    **tools: Any,
) -> tuple[Server, Shop]:
    plugin = plugin or Shop()
    ctx = PluginContext(
        name="p",
        namespace="p",
        config=NoConfig(),
        http=httpx.AsyncClient(),
        log=logging.getLogger("any_mcp.plugin.p"),
        response_budget_chars=1000,
    )
    plugin.ctx = ctx
    config = AnyMcpConfig.model_validate(
        {
            "server": {"name": "shop-server", "instructions": "Use the shop tools."},
            "tools": tools,
            "plugins": [{"name": "p", "plugin": "tests:Shop"}],
        }
    )
    entry = PluginEntry(name="p", plugin="tests:Shop")
    registry = ToolRegistry([LoadedPlugin(entry, plugin, ctx, 30.0, reload_every)], config.tools)
    return build_server(registry, config), plugin


async def _eventually(check: Callable[[], bool], timeout: float = 3.0) -> None:
    async def wait() -> None:
        while not check():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


ERAS = (False, True)  # modern (2026-07-28), legacy (initialize handshake)


async def test_list_tools_matches_registry() -> None:
    server, _ = _server()
    for legacy in ERAS:
        async with in_memory_client(server, legacy=legacy) as client:
            tools = await client.list_tools()
            registry_tools = server.registry.tools()
            names = [t["name"] for t in tools]
            assert names == ["p_boom", "p_echo", "p_pair"]
            assert names == [t.name for t in registry_tools]
            for listed, exposed in zip(tools, registry_tools, strict=True):
                assert listed["inputSchema"] == exposed.listed_schema
                assert listed["description"] == exposed.spec.description
                assert listed.get("outputSchema") == exposed.output_schema
            echo = tools[1]
            assert echo["title"] == "Echo"
            assert echo["outputSchema"] == ECHO_OUTPUT
            assert echo["annotations"] == {"readOnlyHint": True, "openWorldHint": False}
            assert tools[0]["inputSchema"] == {"type": "object", "additionalProperties": False}
            # no title is invented for a tool that declares none
            assert "title" not in tools[0]
            assert "annotations" not in tools[0]


async def test_list_result_cacheable() -> None:
    server, _ = _server(list_ttl_ms=1500, cache_scope="private")
    async with in_memory_client(server) as client:
        assert client.protocol_version == "2026-07-28"
        raw = await client.list_tools_result()
        assert raw["ttlMs"] == 1500
        assert raw["cacheScope"] == "private"
        assert [t["name"] for t in raw["tools"]] == ["p_boom", "p_echo", "p_pair"]

    public, _ = _server(list_ttl_ms=0, cache_scope="public")
    async with in_memory_client(public) as client:
        raw = await client.list_tools_result()
        assert raw["ttlMs"] == 0
        assert raw["cacheScope"] == "public"

    # the handshake era has no CacheableResult, so the fields are absent there
    async with in_memory_client(server, legacy=True) as client:
        assert client.protocol_version != "2026-07-28"
        raw = await client.list_tools_result()
        assert "ttlMs" not in raw and "cacheScope" not in raw
        assert len(raw["tools"]) == 3


async def test_call_results_and_errors() -> None:
    server, _ = _server()
    for legacy in ERAS:
        async with in_memory_client(server, legacy=legacy) as client:
            ok = await client.call_tool("p_echo", {"x": 7})
            assert ok == {"text": '{"echo":7}', "structured": {"echo": 7}, "is_error": False}

            pair = await client.call_tool("p_pair")
            # before 2026-07-28 structuredContent must be an object: the array is text only
            expected = None if legacy else [1, 2]
            assert pair == {"text": "[1,2]", "structured": expected, "is_error": False}

            # FastMCP does not pre-empt the pipeline: its validation error is an isError result
            bad = await client.call_tool("p_echo", {"x": "abc", "extra": 1})
            assert bad["is_error"] is True
            assert bad["structured"]["error"]["kind"] == "invalid_arguments"
            assert "$.x" in bad["text"]

            missing = await client.call_tool("p_echo", {})
            assert missing["is_error"] is True
            assert missing["structured"]["error"]["kind"] == "invalid_arguments"

            failed = await client.call_tool("p_boom", {})
            assert failed["is_error"] is True
            assert failed["structured"]["error"]["kind"] == "internal"
            assert failed["text"] == "p failed: RuntimeError"
            assert "secret-detail" not in json.dumps(failed)


async def test_unknown_tool_is_protocol_error() -> None:
    server, _ = _server()
    for legacy in ERAS:
        async with in_memory_client(server, legacy=legacy) as client:
            with pytest.raises(ProtocolError) as info:
                await client.call_tool("p_nope", {})
            assert info.value.code == -32602
            assert "p_nope" in info.value.message
            # the connection stays usable after a protocol error
            assert len(await client.list_tools()) == 3


async def test_reload_visible_on_next_list() -> None:
    server, plugin = _server()
    for legacy in ERAS:
        plugin.specs = _base_specs()
        async with in_memory_client(server, legacy=legacy) as client:
            assert [t["name"] for t in await client.list_tools()] == [
                "p_boom",
                "p_echo",
                "p_pair",
            ]
            plugin.specs = [
                ToolSpec(name="added", description="New tool.", handler=_added),
                *[s for s in _base_specs() if s.name != "boom"],
            ]
            assert await server.registry.reload() is True
            assert [t["name"] for t in await client.list_tools()] == [
                "p_added",
                "p_echo",
                "p_pair",
            ]
            assert (await client.call_tool("p_added"))["structured"] == {"added": True}
            with pytest.raises(ProtocolError) as info:
                await client.call_tool("p_boom", {})
            assert info.value.code == -32602


async def test_notify_only_on_change() -> None:
    server, plugin = _server()
    async with (
        in_memory_client(server, subscribe=True) as modern,
        in_memory_client(server) as quiet,
        in_memory_client(server, legacy=True) as legacy,
    ):
        clients: tuple[InMemoryClient, ...] = (modern, legacy)
        await legacy.list_tools()
        assert await server.registry.reload() is False  # unchanged: nothing is sent
        plugin.specs = _base_specs()[:2]
        assert await server.registry.reload() is True
        await _eventually(lambda: all(len(c.notifications()) == 1 for c in clients))
        assert await server.registry.reload() is False
        plugin.specs = _base_specs()
        assert await server.registry.reload() is True
        await _eventually(lambda: all(len(c.notifications()) == 2 for c in clients))
        await asyncio.sleep(0.1)
        for client in clients:
            assert client.notifications() == [TOOL_CHANGED, TOOL_CHANGED]
        # a 2026-07-28 client that opened no subscriptions/listen stream gets nothing
        assert quiet.notifications() == []
        # and every client sees the current list without acting on any notification
        assert len(await quiet.list_tools()) == 3


async def test_only_tools_capability() -> None:
    server, _ = _server()
    for legacy in ERAS:
        async with in_memory_client(server, legacy=legacy) as client:
            assert client.capabilities() == {"tools": {"listChanged": True}}
            assert client.instructions == "Use the shop tools."
            assert client.server_name == "shop-server"
            for method, params in (
                ("resources/list", {}),
                ("resources/templates/list", {}),
                ("resources/read", {"uri": "file:///etc/hosts"}),
                ("prompts/list", {}),
                ("prompts/get", {"name": "x"}),
                ("logging/setLevel", {"level": "info"}),
            ):
                with pytest.raises(ProtocolError) as info:
                    await client.request(method, params)
                assert info.value.code == -32601


async def test_lifespan_runs_plugin_lifecycle() -> None:
    server, plugin = _server(reload_every=0.02)
    assert plugin.events == []
    async with in_memory_client(server) as client:
        assert plugin.events[:2] == ["setup", "load_tools"]
        assert len(await client.list_tools()) == 3
        await _eventually(lambda: plugin.events.count("load_tools") >= 4)
    assert plugin.events[-1] == "teardown"
    assert plugin.events.count("setup") == 1
    loads = plugin.events.count("load_tools")
    await asyncio.sleep(0.1)
    assert plugin.events.count("load_tools") == loads  # periodic reloads stopped


async def test_run_stdio_serves_tools(tmp_path: Path) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    marker = tmp_path / "events.log"
    script = tmp_path / "serve.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import asyncio, logging, sys
            import httpx
            from any_mcp._fastmcp_adapter import build_server, run_stdio
            from any_mcp.config import AnyMcpConfig, PluginEntry
            from any_mcp.context import NoConfig, PluginContext
            from any_mcp.discovery import LoadedPlugin
            from any_mcp.plugin import Plugin
            from any_mcp.registry import ToolRegistry
            from any_mcp.types import ToolSpec

            MARKER = {str(marker)!r}

            async def hello(call):
                return {{"hello": "world"}}

            class P(Plugin):
                async def load_tools(self):
                    return [ToolSpec(name="hello", description="Say hello.", handler=hello)]

                async def teardown(self):
                    with open(MARKER, "a") as fh:
                        fh.write("teardown\\n")

            config = AnyMcpConfig.model_validate({{"server": {{"name": "stdio-test"}}}})
            ctx = PluginContext("p", "p", NoConfig(), httpx.AsyncClient(),
                                logging.getLogger("p"), 1000)
            plugin = P()
            plugin.ctx = ctx
            loaded = LoadedPlugin(PluginEntry(name="p", plugin="x:P"), plugin, ctx, 30.0, None)
            server = build_server(ToolRegistry([loaded], config.tools), config)
            asyncio.run(run_stdio(server))
            """
        )
    )
    transport = StdioTransport(command=sys.executable, args=[str(script)], keep_alive=False)
    async with Client(transport) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["p_hello"]
        result = await client.call_tool_mcp("p_hello", {})
        assert result.structured_content == {"hello": "world"}
    await _eventually(lambda: marker.exists() and "teardown" in marker.read_text(), 10.0)
