"""Plugin discovery: resolve ``module:Class`` or entry-point references and build instances."""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points

import httpx
from pydantic import BaseModel, ValidationError

from any_mcp.config import AnyMcpConfig, PluginEntry
from any_mcp.context import PluginContext
from any_mcp.errors import ConfigError
from any_mcp.http import make_client
from any_mcp.plugin import Plugin

ENTRY_POINT_GROUP = "any_mcp.plugins"


@dataclass
class LoadedPlugin:
    """A configured plugin instance with its context and effective timing settings."""

    entry: PluginEntry
    instance: Plugin
    ctx: PluginContext
    timeout: float
    reload_every: float | None


def _check(target: object, ref: str) -> type[Plugin]:
    if not (isinstance(target, type) and issubclass(target, Plugin)):
        raise ConfigError(f"plugin {ref!r}: target is not a Plugin subclass")
    return target


def resolve_plugin_class(ref: str, *, python_path: Sequence[str] = ()) -> type[Plugin]:
    """Resolve ``module:Class`` or an entry-point name in group ``any_mcp.plugins``."""
    if ":" in ref:
        for directory in python_path:
            if directory not in sys.path:
                sys.path.insert(0, directory)
        module_name, _, attr = ref.partition(":")
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise ConfigError(
                f"plugin {ref!r}: cannot import module {module_name!r}: {exc!r}"
            ) from exc
        try:
            target = getattr(module, attr)
        except AttributeError:
            raise ConfigError(
                f"plugin {ref!r}: module {module_name!r} has no attribute {attr!r}"
            ) from None
        return _check(target, ref)
    matches = list(entry_points(group=ENTRY_POINT_GROUP, name=ref))
    if not matches:
        raise ConfigError(
            f"plugin {ref!r}: no entry point of that name in group {ENTRY_POINT_GROUP!r}"
        )
    try:
        target = matches[0].load()
    except Exception as exc:
        raise ConfigError(f"plugin {ref!r}: entry point failed to load: {exc}") from exc
    return _check(target, ref)


def _validate_config(cls: type[Plugin], entry: PluginEntry, index: int) -> BaseModel:
    try:
        return cls.Config.model_validate(entry.config)
    except ValidationError as exc:
        problems = "; ".join(
            ".".join([f"plugins[{index}]", "config", *map(str, e["loc"])]) + f": {e['msg']}"
            for e in exc.errors()
        )
        raise ConfigError(f"plugin {entry.name!r}: {problems}") from None


def load_plugins(
    config: AnyMcpConfig, *, http_transport: httpx.AsyncBaseTransport | None = None
) -> list[LoadedPlugin]:
    """Instantiate every configured plugin with its context, in config order."""
    loaded: list[LoadedPlugin] = []
    for index, entry in enumerate(config.plugins):
        cls = resolve_plugin_class(entry.plugin, python_path=config.python_path)
        validated = _validate_config(cls, entry, index)
        timeout = entry.timeout if entry.timeout is not None else cls.timeout
        reload_every = entry.reload_every if entry.reload_every is not None else cls.reload_every
        base_url = getattr(validated, "base_url", None)
        ctx = PluginContext(
            name=entry.name,
            namespace=entry.namespace if entry.namespace is not None else entry.name,
            config=validated,
            http=make_client(
                str(base_url) if base_url is not None else "",
                transport=http_transport,
                timeout=timeout,
            ),
            log=logging.getLogger(f"any_mcp.plugin.{entry.name}"),
            response_budget_chars=config.tools.response_budget_chars,
        )
        instance = cls()
        instance.ctx = ctx
        loaded.append(LoadedPlugin(entry, instance, ctx, timeout, reload_every))
    return loaded
