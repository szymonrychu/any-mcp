"""Plugin discovery: resolve ``module:Class``, entry-point or source references, build instances.

A ``source`` entry is installed through ``any_mcp.install`` when the manifest does not list it
yet (no refresh), then the chosen ``any_mcp.plugins`` entry point of its distribution is loaded.
"""

from __future__ import annotations

import importlib
import logging
import site
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

from any_mcp import install as installer
from any_mcp.config import AnyMcpConfig, PluginEntry
from any_mcp.context import PluginContext
from any_mcp.errors import ConfigError
from any_mcp.http import make_client
from any_mcp.install import ENTRY_POINT_GROUP, Installed
from any_mcp.plugin import Plugin
from any_mcp.sources import parse_source


@dataclass
class LoadedPlugin:
    """A configured plugin instance with its context and effective timing settings."""

    entry: PluginEntry
    instance: Plugin
    ctx: PluginContext
    timeout: float
    reload_every: float | None


class LoadedPlugins(list[LoadedPlugin]):
    """The plugins ``load_plugins`` built, which also own their HTTP clients.

    The owner of this list calls ``aclose()`` once at final shutdown; a registry restart must
    not, because the clients are reused by the next start. Closing twice is a no-op.
    """

    async def aclose(self) -> None:
        """Close every per-plugin ``httpx.AsyncClient``; safe to call again."""
        first: Exception | None = None
        for plugin in self:
            if plugin.ctx.http.is_closed:
                continue
            try:
                await plugin.ctx.http.aclose()
            except Exception as exc:
                first = first or exc
        if first is not None:
            raise first


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


def _activate_installs() -> None:
    """Process the ``.pth`` files of this environment's site directories again.

    The interpreter reads them only at startup; an editable install (a local source) adds its
    path through one, so a source installed by this process needs it to become importable.
    Paths already on ``sys.path`` are skipped.
    """
    for directory in site.getsitepackages():
        site.addsitedir(directory)
    importlib.invalidate_caches()


def _source_error(found: Installed, reason: str) -> ConfigError:
    return ConfigError(f"plugin source '{found.source.raw}': {reason}")


def _entry_point_name(entry: PluginEntry, found: Installed) -> str:
    names = ", ".join(sorted(found.entry_points))
    if entry.plugin is not None:
        if entry.plugin not in found.entry_points:
            raise _source_error(
                found,
                f"distribution '{found.distribution}' has no '{ENTRY_POINT_GROUP}' entry point "
                f"'{entry.plugin}'; it declares: {names}",
            )
        return entry.plugin
    if len(found.entry_points) > 1:
        raise _source_error(
            found,
            f"distribution '{found.distribution}' declares several '{ENTRY_POINT_GROUP}' entry "
            f"points ({names}); set plugins[].plugin to one of them",
        )
    return next(iter(found.entry_points))


def resolve_source_plugin(entry: PluginEntry, *, base_dir: Path) -> type[Plugin]:
    """Install ``entry.source`` unless the manifest lists it, then load its chosen entry point."""
    assert entry.source is not None
    source = parse_source(entry.source)
    found = installer.installed(source, base_dir=base_dir)
    if found is None:
        found = installer.install(source, base_dir=base_dir, refresh=False)
        _activate_installs()
    name = _entry_point_name(entry, found)
    value = found.entry_points[name]
    try:
        target = EntryPoint(name, value, ENTRY_POINT_GROUP).load()
    except Exception as exc:
        raise _source_error(
            found, f"entry point '{name}' ({value}) failed to load: {exc!r}"
        ) from exc
    if not (isinstance(target, type) and issubclass(target, Plugin)):
        raise _source_error(found, f"entry point '{name}' ({value}) is not a Plugin subclass")
    return target


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
) -> LoadedPlugins:
    """Instantiate every configured plugin with its context, in config order."""
    loaded = LoadedPlugins()
    for index, entry in enumerate(config.plugins):
        if entry.source is not None:
            cls = resolve_source_plugin(entry, base_dir=config.base_dir or Path.cwd())
        else:
            assert entry.plugin is not None
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
            config_dir=config.base_dir,
        )
        instance = cls()
        instance.ctx = ctx
        loaded.append(LoadedPlugin(entry, instance, ctx, timeout, reload_every))
    return loaded
