from __future__ import annotations

import dataclasses
import logging

import httpx
import pytest

import any_mcp
from any_mcp.config import PluginEntry, ToolSettings
from any_mcp.context import NoConfig, PluginContext
from any_mcp.discovery import LoadedPlugin
from any_mcp.pipeline import call_tool
from any_mcp.plugin import Plugin
from any_mcp.registry import ToolRegistry
from any_mcp.types import InputRequest, InputRequired, ToolCall, ToolSpec


class Asker(Plugin):
    def __init__(self, *, ask: bool = True, fail: bool = False) -> None:
        self.ask = ask
        self.fail = fail
        self.calls: list[ToolCall] = []
        self.asked: list[ToolCall] = []

    async def load_tools(self):
        async def handler(call: ToolCall):
            self.calls.append(call)
            return {"ok": True}

        return [ToolSpec(name="t", description="T.", handler=handler)]

    async def request_input(self, call: ToolCall) -> InputRequired | None:
        self.asked.append(call)
        if self.fail:
            raise RuntimeError("hook boom")
        if not self.ask or "confirm" in call.input_responses:
            return None
        return InputRequired(
            {"confirm": InputRequest("elicitation", "Proceed?", {"type": "object"})},
            state="s1",
        )


async def _registry(plugin: Plugin) -> ToolRegistry:
    ctx = PluginContext(
        name="p",
        namespace="p",
        config=NoConfig(),
        http=httpx.AsyncClient(),
        log=logging.getLogger("any_mcp.plugin.p"),
        response_budget_chars=1000,
    )
    plugin.ctx = ctx
    entry = PluginEntry(name="p", plugin="tests:Asker")
    registry = ToolRegistry([LoadedPlugin(entry, plugin, ctx, 30.0, None)], ToolSettings())
    await registry.start()
    return registry


async def test_default_never_requests_input() -> None:
    class Plain(Plugin):
        async def load_tools(self):
            async def handler(call: ToolCall):
                return {"ok": 1}

            return [ToolSpec(name="t", description="T.", handler=handler)]

    plugin = Plain()
    registry = await _registry(plugin)
    result = await call_tool(registry, "p_t", {})
    assert not result.is_error and result.input_required is None
    call = ToolCall("p", "t", "p_t", {}, (await plugin.load_tools())[0], "r")
    assert await plugin.request_input(call) is None
    assert call.input_responses == {} and call.request_state is None


async def test_input_required_short_circuits_call() -> None:
    plugin = Asker()
    registry = await _registry(plugin)
    result = await call_tool(registry, "p_t", {})
    assert not result.is_error
    assert result.input_required is not None
    assert result.input_required.state == "s1"
    assert result.text == "Proceed?"
    assert plugin.calls == []


async def test_retry_carries_responses_and_state() -> None:
    plugin = Asker()
    registry = await _registry(plugin)
    result = await call_tool(
        registry, "p_t", {}, input_responses={"confirm": {"ok": True}}, request_state="s1"
    )
    assert result.input_required is None and not result.is_error
    assert plugin.asked[0].input_responses == {"confirm": {"ok": True}}
    assert plugin.asked[0].request_state == "s1"
    assert plugin.calls[0].input_responses == {"confirm": {"ok": True}}
    assert plugin.calls[0].request_state == "s1"


async def test_request_input_failure_goes_to_on_error() -> None:
    plugin = Asker(fail=True)
    registry = await _registry(plugin)
    result = await call_tool(registry, "p_t", {})
    assert result.is_error and result.input_required is None
    assert plugin.calls == []


def test_types_and_exports() -> None:
    assert any_mcp.InputRequired is InputRequired
    assert any_mcp.InputRequest is InputRequest
    with pytest.raises(ValueError):
        InputRequired({})
    req = InputRequest("elicitation", "m", {"type": "object"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.message = "x"  # type: ignore[misc]
    assert InputRequired({"a": req}).state is None
