"""An in-memory harness that drives a plugin through the real any-mcp server (design D13).

The harness builds the same config, loader, registry and server a deployment uses and
talks to it with an in-process MCP client, so no port is opened. Pass an
``httpx.MockTransport`` as ``transport`` and every ``ctx.http`` request is routed to it.
It needs no pytest and imports FastMCP only through the adapter module::

    async with PluginHarness(MyPlugin, config={"base_url": "https://api.test"},
                             transport=httpx.MockTransport(handler)) as harness:
        result = await harness.call("get_thing", {"id": 1})
"""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from itertools import count
from os import PathLike
from typing import Any

import httpx
from pydantic import ValidationError

from any_mcp._fastmcp_adapter import InMemoryClient, Server, build_server, in_memory_client
from any_mcp.config import AnyMcpConfig, load_config
from any_mcp.discovery import load_plugins
from any_mcp.errors import ConfigError
from any_mcp.plugin import Plugin
from any_mcp.registry import ToolRegistry

__all__ = ["HarnessResult", "PluginHarness"]

_serial = count(1)


@dataclass(frozen=True)
class HarnessResult:
    """A tool call as the client saw it."""

    text: str
    structured: Any
    is_error: bool


def _reference(plugin: type[Plugin] | str) -> tuple[str, str | None]:
    """``(reference, synthetic module name)`` for a plugin class or reference string."""
    if isinstance(plugin, str):
        return plugin, None
    if not (isinstance(plugin, type) and issubclass(plugin, Plugin)):
        raise ConfigError(f"plugin {plugin!r}: not a Plugin subclass or reference string")
    module = sys.modules.get(plugin.__module__)
    if module is not None and getattr(module, plugin.__qualname__, None) is plugin:
        return f"{plugin.__module__}:{plugin.__qualname__}", None
    # a class defined inside a function: make it importable under a throwaway module
    synthetic = f"_any_mcp_harness_{next(_serial)}"
    holder = types.ModuleType(synthetic)
    holder.Plugin = plugin  # type: ignore[attr-defined]
    sys.modules[synthetic] = holder
    return f"{synthetic}:Plugin", synthetic


def _default_name(ref: str) -> str:
    return (ref.rsplit(":", 1)[1] if ":" in ref else ref).lower().replace(".", "_")


class PluginHarness:
    """Serve plugins in memory; an async context manager that starts and stops them."""

    def __init__(
        self,
        plugin: type[Plugin] | str,
        *,
        config: Mapping[str, Any] | None = None,
        name: str | None = None,
        namespace: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        tools: list[str] | None = None,
    ) -> None:
        ref, self._synthetic = _reference(plugin)
        entry: dict[str, Any] = {
            "name": name or _default_name(ref),
            "plugin": ref,
            "config": dict(config or {}),
        }
        if namespace is not None:
            entry["namespace"] = namespace
        if tools is not None:
            entry["allow"] = list(tools)
        self._config = _build_config({"plugins": [entry]})
        self._transport = transport
        self._stack: AsyncExitStack | None = None
        self._server: Server | None = None
        self._client: InMemoryClient | None = None

    @classmethod
    def from_config(
        cls, path: str | PathLike[str], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> PluginHarness:
        """Serve every plugin of a config file; relative ``python_path`` resolves against it."""
        harness = cls.__new__(cls)
        harness._synthetic = None
        harness._config = load_config(path)
        harness._transport = transport
        harness._stack = None
        harness._server = None
        harness._client = None
        return harness

    async def __aenter__(self) -> PluginHarness:
        stack = AsyncExitStack()
        try:
            if self._synthetic is not None:
                stack.callback(sys.modules.pop, self._synthetic, None)
            loaded = load_plugins(self._config, http_transport=self._transport)
            for plugin in loaded:
                stack.push_async_callback(plugin.ctx.http.aclose)
            self._server = build_server(ToolRegistry(loaded, self._config.tools), self._config)
            self._client = await stack.enter_async_context(in_memory_client(self._server))
        except BaseException as exc:
            await stack.aclose()
            config_error = _find_config_error(exc)
            if config_error is not None and config_error is not exc:
                raise config_error from None
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *exc: object) -> None:
        stack, self._stack = self._stack, None
        self._client = None
        if stack is not None:
            await stack.aclose()

    def _live(self) -> tuple[Server, InMemoryClient]:
        if self._server is None or self._client is None:
            raise RuntimeError("the harness is not entered; use 'async with'")
        return self._server, self._client

    async def list_tools(self) -> list[dict[str, Any]]:
        """Tools as a client sees them (MCP tool dicts)."""
        return await self._live()[1].list_tools()

    def _exposed(self, tool: str) -> str:
        registry = self._live()[0].registry
        if registry.get(tool) is not None:
            return tool
        local = [t.name for t in registry.tools() if t.spec.name == tool]
        if len(local) == 1:
            return local[0]
        return tool  # unknown or ambiguous: let the server answer

    async def call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> HarnessResult:
        """Call a tool by exposed name, or by plugin-local name when that is unambiguous."""
        client = self._live()[1]
        raw = await client.call_tool(self._exposed(tool), arguments)
        return HarnessResult(raw["text"], raw["structured"], raw["is_error"])

    async def reload(self) -> bool:
        """Reload every plugin; True when the tool list changed."""
        return await self._live()[0].registry.reload()


def _find_config_error(exc: BaseException) -> ConfigError | None:
    """The ConfigError the client wrapped on connect (via cause or context), if any."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ConfigError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _build_config(data: dict[str, Any]) -> AnyMcpConfig:
    try:
        return AnyMcpConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"harness config: {problems}") from None
