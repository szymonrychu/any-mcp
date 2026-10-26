"""The ``any-mcp`` command: load a config or plugins, then serve over stdio or HTTP.

``any-mcp plugins install`` installs plugin sources ahead of the first start.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from any_mcp import __version__
from any_mcp import install as installer
from any_mcp.config import (
    AnyMcpConfig,
    HttpSettings,
    config_from_plugins,
    load_config,
    load_sources,
)
from any_mcp.discovery import load_plugins
from any_mcp.errors import ConfigError
from any_mcp.log import LEVELS, configure
from any_mcp.sources import parse_source

EXIT_CONFIG = 78
ENV_CONFIG = "ANY_MCP_CONFIG"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="any-mcp", description="Expose any API as MCP tools through plugins."
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=os.environ.get(ENV_CONFIG) or None,
        help=f"YAML or JSON config file (default: ${ENV_CONFIG})",
    )
    parser.add_argument(
        "--plugin",
        metavar="REF",
        action="append",
        default=[],
        help="plugin as module:Class or entry-point name; repeatable, appended to the config",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,
        help="transport (default: from the config, else stdio)",
    )
    parser.add_argument("--host", default=None, help="HTTP bind host (default: server.http.host)")
    parser.add_argument(
        "--port", type=int, default=None, help="HTTP bind port (default: server.http.port)"
    )
    parser.add_argument(
        "--path", default=None, help="HTTP endpoint path (default: server.http.path)"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=None,
        help="allow HTTP on a non-loopback host without a bearer token (default: "
        "server.http.insecure)",
    )
    parser.add_argument(
        "--log-level", choices=list(LEVELS), default=None, help="log level (default: from config)"
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="print the exposed tools as a JSON array and exit",
    )
    parser.add_argument("--version", action="version", version=f"any-mcp {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    plugins = commands.add_parser("plugins", help="manage plugin sources")
    actions = plugins.add_subparsers(dest="action", metavar="ACTION", required=True)
    install = actions.add_parser(
        "install",
        help="install plugin sources into this environment, refreshing branch and tag refs",
        description="Install each SOURCE (relative paths resolve against the working directory) "
        "and every plugins[].source of --config (relative to the config file).",
    )
    install.add_argument(
        "--config",
        dest="install_config",
        metavar="PATH",
        default=None,
        help="config file whose plugins[].source entries to install",
    )
    install.add_argument("sources", metavar="SOURCE", nargs="*", help="a plugin source")
    return parser


def _fail(message: str) -> int:
    print(f"any-mcp: {' '.join(message.split())}", file=sys.stderr)
    return EXIT_CONFIG


def _build_config(args: argparse.Namespace) -> AnyMcpConfig:
    if not args.config and not args.plugin:
        raise ConfigError("no config: pass --config, --plugin or set ANY_MCP_CONFIG")
    if args.config:
        config = load_config(args.config)
        if args.plugin:
            extra = config_from_plugins(args.plugin)
            config.plugins.extend(extra.plugins)
            names = [p.name for p in config.plugins]
            for name in {n for n in names if names.count(n) > 1}:
                raise ConfigError(f"--plugin: duplicate plugin name '{name}'")
        return config
    return config_from_plugins(args.plugin)


async def _list_tools(config: AnyMcpConfig) -> list[dict[str, object]]:
    from any_mcp.registry import ToolRegistry

    loaded = load_plugins(config)
    registry = ToolRegistry(loaded, config.tools)
    try:
        await registry.start()
        return [
            {"name": t.name, "description": t.spec.description, "inputSchema": t.listed_schema}
            for t in registry.tools()
        ]
    finally:
        await registry.stop()
        await loaded.aclose()


async def _serve_stdio(config: AnyMcpConfig) -> bool:
    """Serve until stdin closes (False) or SIGTERM/SIGINT arrives (True).

    The stdin read blocks a worker thread that cancellation cannot interrupt, so on a signal
    the plugins are torn down here and the caller must leave the process without waiting for
    that thread.
    """
    from any_mcp._fastmcp_adapter import build_server, run_stdio
    from any_mcp.registry import ToolRegistry

    loaded = load_plugins(config)
    registry = ToolRegistry(loaded, config.tools)
    task = asyncio.ensure_future(run_stdio(build_server(registry, config, aclose=loaded.aclose)))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGINT, stop.set)
    waiter = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            await task  # stdin closed (or the server failed): the lifespan tore the plugins down
            return False
        task.cancel()
        await registry.stop()
        await asyncio.wait({task}, timeout=1)  # not wait_for: that would wait on the blocked read
        return True
    finally:
        waiter.cancel()
        await loaded.aclose()


def _http_settings(config: AnyMcpConfig, args: argparse.Namespace) -> HttpSettings:
    flags = ("host", "port", "path", "insecure")
    overrides = {k: v for k in flags if (v := getattr(args, k)) is not None}
    try:
        return HttpSettings.model_validate({**config.server.http.model_dump(), **overrides})
    except ValidationError as exc:
        raise ConfigError(f"--host/--port/--path: {exc.errors()[0]['msg']}") from None


async def _serve_http(config: AnyMcpConfig, settings: HttpSettings) -> None:
    from any_mcp._fastmcp_adapter import build_server, run_http
    from any_mcp.registry import ToolRegistry

    loaded = load_plugins(config)
    registry = ToolRegistry(loaded, config.tools)
    try:
        await run_http(build_server(registry, config, aclose=loaded.aclose), settings)
    finally:
        await loaded.aclose()


def _config_error(exc: BaseException) -> ConfigError | None:
    if isinstance(exc, ConfigError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _config_error(inner)
            if found is not None:
                return found
    return None


def _install_sources(args: argparse.Namespace) -> None:
    """Install every given source and every source of ``--config``, one stdout line each."""
    if not args.sources and not args.install_config:
        raise ConfigError("plugins install: pass a SOURCE or --config")
    texts = [(text, Path.cwd()) for text in args.sources]
    if args.install_config:
        found, base_dir = load_sources(args.install_config)
        texts += [(text, base_dir) for text in found]
    jobs = [(parse_source(text), base_dir) for text, base_dir in texts]
    for source, base_dir in jobs:
        done = installer.install(source, base_dir=base_dir, refresh=True)
        print(f"{source.raw} {done.distribution} {done.sha or 'local'}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plugins":
        try:
            _install_sources(args)
        except ConfigError as exc:
            return _fail(str(exc))
        return 0
    try:
        config = _build_config(args)
        transport = args.transport or config.server.transport
        settings = _http_settings(config, args)
        try:
            configure(args.log_level or config.server.log_level)
        except ValueError as exc:
            raise ConfigError(f"server.log_level: {exc}") from None
        if args.list_tools:
            tools = asyncio.run(_list_tools(config))
            sys.stdout.write(json.dumps(tools, indent=2) + "\n")
            sys.stdout.flush()
            return 0
        if transport == "http":
            from any_mcp._fastmcp_adapter import check_http_exposure

            check_http_exposure(settings)
            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(_serve_http(config, settings))
            return 0
        if asyncio.run(_serve_stdio(config)):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)  # a worker thread is still blocked reading stdin
    except (ConfigError, BaseExceptionGroup) as exc:
        found = _config_error(exc)
        if found is None:
            raise
        return _fail(str(found))
    return 0
