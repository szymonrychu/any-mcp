"""The ``any-mcp`` command: load a config or plugins, then serve over stdio or HTTP."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from any_mcp import __version__
from any_mcp.config import AnyMcpConfig, HttpSettings, config_from_plugins, load_config
from any_mcp.discovery import load_plugins
from any_mcp.errors import ConfigError
from any_mcp.log import LEVELS, configure

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
        "--log-level", choices=list(LEVELS), default=None, help="log level (default: from config)"
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="print the exposed tools as a JSON array and exit",
    )
    parser.add_argument("--version", action="version", version=f"any-mcp {__version__}")
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

    registry = ToolRegistry(load_plugins(config), config.tools)
    await registry.start()
    try:
        return [
            {"name": t.name, "description": t.spec.description, "inputSchema": t.listed_schema}
            for t in registry.tools()
        ]
    finally:
        await registry.stop()


async def _serve_stdio(config: AnyMcpConfig) -> bool:
    """Serve until stdin closes (False) or SIGTERM/SIGINT arrives (True).

    The stdin read blocks a worker thread that cancellation cannot interrupt, so on a signal
    the plugins are torn down here and the caller must leave the process without waiting for
    that thread.
    """
    from any_mcp._fastmcp_adapter import build_server, run_stdio
    from any_mcp.registry import ToolRegistry

    registry = ToolRegistry(load_plugins(config), config.tools)
    task = asyncio.ensure_future(run_stdio(build_server(registry, config)))
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


def _http_settings(config: AnyMcpConfig, args: argparse.Namespace) -> HttpSettings:
    overrides = {k: v for k in ("host", "port", "path") if (v := getattr(args, k)) is not None}
    try:
        return HttpSettings.model_validate({**config.server.http.model_dump(), **overrides})
    except ValidationError as exc:
        raise ConfigError(f"--host/--port/--path: {exc.errors()[0]['msg']}") from None


async def _serve_http(config: AnyMcpConfig, settings: HttpSettings) -> None:
    from any_mcp._fastmcp_adapter import build_server, run_http
    from any_mcp.registry import ToolRegistry

    registry = ToolRegistry(load_plugins(config), config.tools)
    await run_http(build_server(registry, config), settings)


def _config_error(exc: BaseException) -> ConfigError | None:
    if isinstance(exc, ConfigError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _config_error(inner)
            if found is not None:
                return found
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
