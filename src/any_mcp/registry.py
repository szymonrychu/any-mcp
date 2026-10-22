"""The global tool list: every loaded plugin's tools, named, filtered, normalised and reloadable."""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from any_mcp.config import ToolSettings
from any_mcp.discovery import LoadedPlugin
from any_mcp.errors import ConfigError
from any_mcp.schema import SchemaError, exposed_name, listing_schema, normalize_schema
from any_mcp.types import ToolSpec, _canonical, _thaw

log = logging.getLogger("any_mcp.registry")

Listener = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ExposedTool:
    """One tool as served: its exposed name, owning plugin, spec and normalised schemas.

    ``input_schema`` is what arguments are validated against; ``listed_schema`` is what
    ``tools/list`` shows (a root ``anyOf``/``oneOf`` of objects flattened into one object).
    """

    name: str
    plugin: LoadedPlugin
    spec: ToolSpec
    input_schema: dict[str, Any]
    listed_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


def _describe(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _signature(tools: Iterable[ExposedTool]) -> tuple[Any, ...]:
    """What a reload compares: names, descriptions, schemas, tags and annotations."""
    return tuple(
        (t.name, t.plugin.entry.name, t.spec.signature(), _canonical(t.listed_schema))
        for t in tools
    )


class ToolRegistry:
    """One global, sorted tool list over ``plugins``; no method depends on a caller."""

    def __init__(self, plugins: Sequence[LoadedPlugin], settings: ToolSettings) -> None:
        self._plugins = list(plugins)
        self._by_name = {p.entry.name: p for p in self._plugins}
        self._settings = settings
        self._lock = asyncio.Lock()
        self._listeners: list[Listener] = []
        self._set_up: list[LoadedPlugin] = []
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._per_plugin: dict[str, list[ExposedTool]] = {}
        # (sorted tools, name index), replaced by one assignment so readers never see a mix
        self._view: tuple[tuple[ExposedTool, ...], Mapping[str, ExposedTool]] = ((), {})
        self._periodic: set[asyncio.Task[None]] = set()
        self._requests: set[asyncio.Task[None]] = set()

    # -- reading ---------------------------------------------------------------------------

    def tools(self) -> list[ExposedTool]:
        """Every exposed tool, sorted by name."""
        return list(self._view[0])

    def get(self, name: str) -> ExposedTool | None:
        return self._view[1].get(name)

    def add_listener(self, cb: Listener) -> None:
        """``cb`` is awaited after every reload that really changed the tool list."""
        self._listeners.append(cb)

    # -- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        """``setup()`` then the first ``load_tools()`` of each plugin, in order.

        Any failure tears down the plugins already set up (in reverse) and raises
        ``ConfigError`` naming the plugin.
        """
        async with self._lock:
            if self._started:
                raise RuntimeError("registry already started")
            self._loop = asyncio.get_running_loop()
            per_plugin: dict[str, list[ExposedTool]] = {}
            try:
                for plugin in self._plugins:
                    name = plugin.entry.name
                    plugin.ctx._bind_reload(partial(self.request_reload, name))
                    try:
                        await plugin.instance.setup()
                    except Exception as exc:
                        raise ConfigError(
                            f"plugin {name!r}: setup failed: {_describe(exc)}"
                        ) from exc
                    self._set_up.append(plugin)
                    try:
                        specs = await plugin.instance.load_tools()
                    except Exception as exc:
                        raise ConfigError(
                            f"plugin {name!r}: load_tools failed: {_describe(exc)}"
                        ) from exc
                    per_plugin[name] = self._build(plugin, specs)
                tools = self._merge(per_plugin)
            except BaseException:
                await self._teardown()
                raise
            self._per_plugin = per_plugin
            self._swap(tools)
            self._started = True

    async def stop(self) -> None:
        """Cancel periodic and requested reloads, then tear plugins down in reverse order."""
        self._started = False
        pending = [*self._periodic, *self._requests]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        while self._set_up:
            plugin = self._set_up.pop()
            try:
                await plugin.instance.teardown()
            except Exception as exc:
                log.error("plugin %r: teardown failed: %s", plugin.entry.name, _describe(exc))

    # -- reload ----------------------------------------------------------------------------

    async def reload(self, plugin: str | None = None) -> bool:
        """Reload one plugin (by entry name) or all; True only when the tool list changed.

        A plugin whose ``load_tools()`` raises, or whose new tools are invalid or collide,
        keeps its previous tools and logs one line. Listeners are awaited on a real change.
        """
        if plugin is not None and plugin not in self._by_name:
            raise ConfigError(f"unknown plugin {plugin!r}")
        async with self._lock:
            if not self._started:
                raise RuntimeError("registry is not started")
            old = self._view[0]
            per_plugin = dict(self._per_plugin)
            tools = list(old)
            for target in self._plugins:
                name = target.entry.name
                if plugin is not None and name != plugin:
                    continue
                try:
                    specs = await target.instance.load_tools()
                    candidate = {**per_plugin, name: self._build(target, specs)}
                    tools = self._merge(candidate)
                except Exception as exc:
                    log.error(
                        "plugin %r: reload failed, keeping its previous tools: %s",
                        name,
                        _describe(exc),
                    )
                    continue
                per_plugin = candidate
            changed = _signature(tools) != _signature(old)
            self._per_plugin = per_plugin
            self._swap(tools)
        if changed:
            for listener in list(self._listeners):
                try:
                    await listener()
                except Exception as exc:
                    log.error("tool list change listener failed: %s", _describe(exc))
        return changed

    async def run_periodic(self) -> None:
        """Reload each plugin with ``reload_every`` at that interval until ``stop()``."""
        if not self._started:
            return
        tasks = {
            asyncio.create_task(self._every(p.entry.name, p.reload_every))
            for p in self._plugins
            if p.reload_every
        }
        self._periodic |= tasks
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                task.cancel()
            self._periodic -= tasks

    async def _every(self, name: str, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await self._reload_logged(name)

    def request_reload(self, plugin: str) -> None:
        """Schedule ``reload(plugin)`` as a task; safe to call from another thread."""
        if plugin not in self._by_name:
            raise ConfigError(f"unknown plugin {plugin!r}")
        if not self._started or self._loop is None:
            log.debug("reload of plugin %r ignored: registry not running", plugin)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._spawn(plugin)
        else:
            self._loop.call_soon_threadsafe(self._spawn, plugin)

    def _spawn(self, plugin: str) -> None:
        if not self._started:
            return
        task = asyncio.create_task(self._reload_logged(plugin))
        self._requests.add(task)
        task.add_done_callback(self._requests.discard)

    async def _reload_logged(self, plugin: str) -> None:
        try:
            await self.reload(plugin)
        except Exception as exc:
            log.error("plugin %r: reload failed: %s", plugin, _describe(exc))

    # -- building --------------------------------------------------------------------------

    def _swap(self, tools: Sequence[ExposedTool]) -> None:
        ordered = tuple(tools)
        self._view = (ordered, {t.name: t for t in ordered})

    def _build(self, plugin: LoadedPlugin, specs: Iterable[ToolSpec]) -> list[ExposedTool]:
        entry = plugin.entry
        tags = set(entry.tags)
        built: list[ExposedTool] = []
        for spec in specs:
            if entry.allow and not any(fnmatch.fnmatchcase(spec.name, g) for g in entry.allow):
                continue
            if tags and not tags & spec.tags:
                continue
            try:
                input_schema = normalize_schema(spec.json_schema())
                listed = copy.deepcopy(listing_schema(input_schema))
            except SchemaError as exc:
                raise ConfigError(
                    f"plugin {entry.name!r} tool {spec.name!r}: invalid input schema: {exc}"
                ) from exc
            output = _thaw(spec.output_schema) if spec.output_schema is not None else None
            built.append(
                ExposedTool(
                    name=exposed_name(
                        plugin.ctx.namespace, spec.name, max_length=self._settings.max_name_length
                    ),
                    plugin=plugin,
                    spec=spec,
                    input_schema=input_schema,
                    listed_schema=listed,
                    output_schema=output,
                )
            )
        return built

    def _merge(self, per_plugin: Mapping[str, Sequence[ExposedTool]]) -> list[ExposedTool]:
        """All tools sorted by exposed name; a shared exposed name raises ConfigError."""
        index: dict[str, ExposedTool] = {}
        for tools in per_plugin.values():
            for tool in tools:
                first = index.get(tool.name)
                if first is not None:
                    raise ConfigError(
                        f"exposed tool name {tool.name!r} is used by plugin "
                        f"{first.plugin.entry.name!r} tool {first.spec.name!r} and plugin "
                        f"{tool.plugin.entry.name!r} tool {tool.spec.name!r}"
                    )
                index[tool.name] = tool
        return [index[name] for name in sorted(index)]
