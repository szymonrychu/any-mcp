from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

import httpx
import pytest

from any_mcp.config import PluginEntry, ToolSettings
from any_mcp.context import NoConfig, PluginContext
from any_mcp.discovery import LoadedPlugin
from any_mcp.errors import Deny, UnknownTool, UpstreamError
from any_mcp.pipeline import call_tool
from any_mcp.plugin import Plugin
from any_mcp.registry import ToolRegistry
from any_mcp.types import RawResponse, ToolCall, ToolResult, ToolSpec

SECRET = "s3cr3t-marker-value"


class Rec(Plugin):
    def __init__(self, events: list[str], *, timeout: float = 30.0) -> None:
        self.events = events
        self.seen: list[Mapping[str, object]] = []
        self.mode = "ok"
        self.fail_on_error = False
        self.sleep = 0.0
        self.timeout_value = timeout

    async def load_tools(self):
        async def handler(call: ToolCall):
            self.events.append("call")
            self.seen.append(call.arguments)
            if self.mode == "boom":
                raise RuntimeError(f"boom {SECRET}")
            if self.mode == "reflect":
                body = f'{{"error": "bad token {SECRET} and Bearer {SECRET}"}}'.encode()
                raise UpstreamError(RawResponse(401, {}, body, "application/json"))
            if self.mode == "socket":
                raise TimeoutError("socket")
            if self.mode == "cancel":
                raise asyncio.CancelledError
            if self.sleep and self.mode != "slowshape":
                await asyncio.sleep(self.sleep)
            return {"echo": call.arguments.get("x")}

        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
            "additionalProperties": False,
        }
        return [ToolSpec(name="echo", description="Echo.", input_schema=schema, handler=handler)]

    async def credentials(self, call: ToolCall):
        self.events.append("credentials")
        return {"Authorization": SECRET}

    async def before_call(self, call: ToolCall) -> ToolCall:
        self.events.append("before_call")
        if self.mode == "deny":
            raise Deny("not allowed")
        return call.with_arguments({"x": 99})

    async def call(self, call: ToolCall) -> RawResponse:
        await self.credentials(call)
        return await super().call(call)

    async def shape_response(self, call: ToolCall, raw: RawResponse) -> ToolResult:
        self.events.append("shape_response")
        if self.sleep and self.mode == "slowshape":
            await asyncio.sleep(self.sleep)
        if self.mode == "shape":
            raise ValueError("shape boom")
        return await super().shape_response(call, raw)

    async def on_error(self, call: ToolCall, exc: Exception) -> ToolResult:
        self.events.append("on_error")
        if self.fail_on_error:
            raise RuntimeError("on_error boom")
        return await super().on_error(call, exc)


async def _registry(plugin: Rec, timeout: float = 30.0) -> ToolRegistry:
    entry = PluginEntry(name="p", plugin="tests:Rec")
    ctx = PluginContext(
        name="p",
        namespace="p",
        config=NoConfig(),
        http=httpx.AsyncClient(),
        log=logging.getLogger("any_mcp.plugin.p"),
        response_budget_chars=1000,
    )
    plugin.ctx = ctx
    registry = ToolRegistry([LoadedPlugin(entry, plugin, ctx, timeout, None)], ToolSettings())
    await registry.start()
    return registry


async def test_unknown_tool() -> None:
    registry = await _registry(Rec([]))
    with pytest.raises(UnknownTool):
        await call_tool(registry, "nope", {})


async def test_invalid_arguments_never_reach_plugin() -> None:
    events: list[str] = []
    registry = await _registry(Rec(events))
    result = await call_tool(registry, "p_echo", {"x": "abc", "extra": 1})
    assert result.is_error
    assert result.structured["error"]["kind"] == "invalid_arguments"
    assert "$.x" in result.text
    assert "before_call" not in events and "call" not in events and "credentials" not in events


async def test_hook_order() -> None:
    events: list[str] = []
    plugin = Rec(events)
    registry = await _registry(plugin)
    result = await call_tool(registry, "p_echo", {"x": 1}, request_id="rid")
    assert not result.is_error
    assert events == ["before_call", "credentials", "call", "shape_response"]
    assert plugin.seen == [{"x": 99}]


async def test_deny() -> None:
    events: list[str] = []
    plugin = Rec(events)
    plugin.mode = "deny"
    registry = await _registry(plugin)
    result = await call_tool(registry, "p_echo", {"x": 1})
    assert result.is_error
    assert result.structured["error"]["kind"] == "denied"
    assert "not allowed" in result.text
    assert "call" not in events


async def test_exceptions_become_error_results() -> None:
    events: list[str] = []
    plugin = Rec(events)
    registry = await _registry(plugin)
    for mode in ("boom", "shape"):
        plugin.mode = mode
        events.clear()
        result = await call_tool(registry, "p_echo", {"x": 1})
        assert result.is_error
        assert "on_error" in events
        assert SECRET not in result.text
    plugin.fail_on_error = True
    result = await call_tool(registry, "p_echo", {"x": 1})
    assert result.is_error
    assert result.structured["error"]["kind"] == "internal"
    plugin.fail_on_error = False
    plugin.mode = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await call_tool(registry, "p_echo", {"x": 1})


async def test_timeout() -> None:
    plugin = Rec([])
    plugin.sleep = 5
    registry = await _registry(plugin, timeout=0.05)
    result = await call_tool(registry, "p_echo", {"x": 1})
    assert result.is_error
    assert result.structured["error"]["kind"] == "timeout"
    assert "0.05s" in result.text


async def test_call_logging_has_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    plugin = Rec([])
    registry = await _registry(plugin)
    caplog.set_level(logging.DEBUG)
    ok = await call_tool(registry, "p_echo", {"x": 1}, request_id="req-ok")
    plugin.mode = "boom"
    bad = await call_tool(registry, "p_echo", {"x": 1}, request_id="req-bad")
    inv = await call_tool(registry, "p_echo", {"x": SECRET}, request_id="req-inv")
    lines = [r.getMessage() for r in caplog.records if r.name == "any_mcp.pipeline"]
    assert len(lines) == 3
    assert "req-ok" in lines[0] and "p_echo" in lines[0] and "ok" in lines[0] and "ms" in lines[0]
    assert "internal" in lines[1]
    assert "invalid_arguments" in lines[2]
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in everything
    assert SECRET not in ok.text + bad.text + inv.text
    await call_tool(registry, "p_echo", {"x": 1})
    last = [r.getMessage() for r in caplog.records if r.name == "any_mcp.pipeline"][-1]
    assert "req=" in last


async def test_plain_timeout_error_is_not_reported_as_deadline() -> None:
    plugin = Rec([])
    plugin.mode = "socket"
    registry = await _registry(plugin, timeout=30)
    result = await call_tool(registry, "p_echo", {"x": 1})
    assert result.is_error
    assert "30s" not in result.text


async def test_shape_response_is_outside_the_call_timeout() -> None:
    plugin = Rec([])
    plugin.mode = "slowshape"
    plugin.sleep = 0.2
    registry = await _registry(plugin, timeout=0.05)
    result = await call_tool(registry, "p_echo", {"x": 1})
    assert not result.is_error


def test_validation_message_masks_only_the_leading_value() -> None:
    from any_mcp.errors import ToolInputError
    from any_mcp.validation import validate_arguments

    schema = {"type": "object", "properties": {"x": {"type": "integer", "minimum": 10}}}
    with pytest.raises(ToolInputError) as info:
        validate_arguments(schema, {"x": 1})
    assert "minimum of 10" in str(info.value.errors)
    assert "<value>0" not in str(info.value.errors)


async def test_error_body_redacts_credentials() -> None:
    plugin = Rec([])
    plugin.mode = "reflect"
    registry = await _registry(plugin)
    result = await call_tool(registry, "p_echo", {"x": 1})
    assert result.is_error
    assert result.structured["error"]["kind"] == "upstream"
    assert result.structured["error"]["status"] == 401
    assert SECRET not in result.text
    assert SECRET not in str(result.structured)
    assert "[redacted]" in result.text and "HTTP 401" in result.text
