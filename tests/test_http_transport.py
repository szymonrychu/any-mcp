"""The streamable HTTP transport: a real server on a loopback port, real MCP clients."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from any_mcp import cli
from any_mcp._fastmcp_adapter import Server, build_http_app, build_server, run_http
from any_mcp.config import AnyMcpConfig, HttpSettings, PluginEntry
from any_mcp.context import NoConfig, PluginContext
from any_mcp.discovery import LoadedPlugin
from any_mcp.errors import ConfigError
from any_mcp.http import make_client
from any_mcp.plugin import Plugin
from any_mcp.registry import ToolRegistry
from any_mcp.types import HttpBinding, ToolCall, ToolSpec

ROOT = Path(__file__).resolve().parent.parent
SECRET = "inbound-secret-token"
COOKIE = "session=inbound-cookie-value"


async def _echo(call: ToolCall) -> Any:
    return {"echo": call.arguments["x"]}


class Api(Plugin):
    async def load_tools(self) -> list[ToolSpec]:
        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
            "additionalProperties": False,
        }
        return [
            ToolSpec(name="echo", description="Echo x.", input_schema=schema, handler=_echo),
            ToolSpec(
                name="fetch",
                description="Fetch from upstream.",
                http=HttpBinding("GET", "/thing"),
            ),
        ]


def _server(settings: dict[str, Any], upstream: list[httpx.Request] | None = None) -> Server:
    seen = upstream if upstream is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    plugin = Api()
    ctx = PluginContext(
        name="p",
        namespace="p",
        config=NoConfig(),
        http=make_client("https://api.test", transport=httpx.MockTransport(handler)),
        log=logging.getLogger("any_mcp.plugin.p"),
        response_budget_chars=10000,
    )
    plugin.ctx = ctx
    config = AnyMcpConfig.model_validate(
        {
            "server": {"transport": "http", "http": settings},
            "plugins": [{"name": "p", "plugin": "tests:Api"}],
        }
    )
    entry = PluginEntry(name="p", plugin="tests:Api")
    registry = ToolRegistry([LoadedPlugin(entry, plugin, ctx, 30.0, None)], config.tools)
    return build_server(registry, config)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def _running(server: Server, **overrides: Any) -> AsyncIterator[str]:
    port = _free_port()
    settings = server.config.server.http.model_copy(update={"port": port, **overrides})
    task = asyncio.ensure_future(run_http(server, settings))
    url = f"http://127.0.0.1:{port}{settings.path}"
    try:
        async with asyncio.timeout(15):
            while True:
                if task.done():
                    task.result()
                try:
                    async with httpx.AsyncClient() as probe:
                        await probe.get(f"http://127.0.0.1:{port}/")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
        yield url
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _post(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    merged = {"Accept": "application/json, text/event-stream", **(headers or {})}
    return httpx.post(url, json=body, headers=merged)


async def _list_and_call(url: str, mode: str, headers: dict[str, str] | None = None) -> None:
    transport = StreamableHttpTransport(url, headers=headers or {})
    async with Client(transport, mode=mode) as client:  # type: ignore[arg-type]
        names = [t.name for t in await client.list_tools()]
        assert names == ["p_echo", "p_fetch"]
        result = await client.call_tool("p_echo", {"x": 4})
        assert result.structured_content == {"echo": 4}


async def test_http_modern_client() -> None:
    server = _server({})
    assert server.config.server.http.host == "127.0.0.1"
    assert server.config.server.http.path == "/mcp"
    async with _running(server) as url:
        assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
        await _list_and_call(url, "auto")


async def test_http_legacy_client() -> None:
    async with _running(_server({})) as url:
        await _list_and_call(url, "legacy")


async def test_origin_validation() -> None:
    server = _server({"allowed_origins": ["https://app.example"]})
    async with _running(server) as url:
        loop = asyncio.get_running_loop()

        def status(origin: str | None) -> int:
            headers = {"Origin": origin} if origin is not None else {}
            return _post(url, headers).status_code

        assert await loop.run_in_executor(None, status, "https://evil.example") == 403
        assert await loop.run_in_executor(None, status, "null") == 403
        assert await loop.run_in_executor(None, status, "http://localhost:3000") != 403
        assert await loop.run_in_executor(None, status, "http://127.0.0.1:9") != 403
        assert await loop.run_in_executor(None, status, "http://[::1]:9") != 403
        assert await loop.run_in_executor(None, status, "https://app.example") != 403
        assert await loop.run_in_executor(None, status, None) != 403


async def test_bearer_token() -> None:
    server = _server({"bearer_token": "s3cret"})
    async with _running(server) as url:
        loop = asyncio.get_running_loop()

        def status(auth: str | None) -> int:
            return _post(url, {"Authorization": auth} if auth else {}).status_code

        assert await loop.run_in_executor(None, status, None) == 401
        assert await loop.run_in_executor(None, status, "Bearer wrong") == 401
        assert await loop.run_in_executor(None, status, "Basic s3cret") == 401
        assert await loop.run_in_executor(None, status, "Bearer s3cret") != 401
        await _list_and_call(url, "auto", {"Authorization": "Bearer s3cret"})


def test_non_loopback_without_token_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import any_mcp._fastmcp_adapter as adapter

    with pytest.raises(ConfigError, match="--insecure"):
        build_http_app(_server({}), HttpSettings(host="0.0.0.0"))

    async def fake_run_http(server: Server, settings: HttpSettings) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr(adapter, "run_http", fake_run_http)
    path = tmp_path / "c.yaml"
    path.write_text(
        f"python_path: ['{ROOT}']\n"
        "server:\n  transport: http\n  http: {host: 0.0.0.0}\n"
        "plugins:\n  - name: p\n    plugin: 'tests.fixtures.stdio_plugin:StdioPlugin'\n",
        encoding="utf-8",
    )
    assert cli.main(["--config", str(path)]) == 78
    err = capsys.readouterr().err
    assert "bearer_token" in err and "--insecure" in err
    assert cli.main(["--config", str(path), "--host", "::"]) == 78


def test_insecure_flag_allows_with_warning(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import any_mcp._fastmcp_adapter as adapter

    with caplog.at_level(logging.WARNING):
        build_http_app(_server({}), HttpSettings(host="0.0.0.0", insecure=True))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "bearer_token" in warnings[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        build_http_app(_server({}), HttpSettings(host="127.0.0.1"))
        build_http_app(_server({}), HttpSettings(host="0.0.0.0", bearer_token="t"))  # type: ignore[arg-type]
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    seen: list[HttpSettings] = []

    async def fake_run_http(server: Server, settings: HttpSettings) -> None:
        seen.append(settings)

    monkeypatch.setattr(adapter, "run_http", fake_run_http)
    path = tmp_path / "c.yaml"
    path.write_text(
        f"python_path: ['{ROOT}']\n"
        "server:\n  transport: http\n  http: {host: 0.0.0.0}\n"
        "plugins:\n  - name: p\n    plugin: 'tests.fixtures.stdio_plugin:StdioPlugin'\n",
        encoding="utf-8",
    )
    assert cli.main(["--config", str(path), "--insecure"]) == 0
    assert seen[-1].insecure is True
    path.write_text(path.read_text().replace("{host: 0.0.0.0}", "{host: 0.0.0.0, insecure: true}"))
    assert cli.main(["--config", str(path)]) == 0


def test_empty_bearer_token_rejected() -> None:
    with pytest.raises(ValueError, match="bearer_token"):
        HttpSettings(host="0.0.0.0", bearer_token="")  # type: ignore[arg-type]


async def test_inbound_credentials_never_forwarded() -> None:
    upstream: list[httpx.Request] = []
    server = _server({}, upstream)
    headers = {"Authorization": f"Bearer {SECRET}", "Cookie": COOKIE, "X-Other": "other-value"}
    async with _running(server) as url:
        transport = StreamableHttpTransport(url, headers=headers)
        async with Client(transport) as client:
            result = await client.call_tool("p_fetch", {})
            assert result.is_error is False
    assert len(upstream) == 1
    request = upstream[0]
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    assert "x-other" not in request.headers
    blob = repr(request.headers.raw) + str(request.url) + request.content.decode()
    assert SECRET not in blob and "inbound-cookie-value" not in blob


def test_no_sse_transport(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--transport", "sse"])
    assert exc.value.code == 2
    assert "sse" in capsys.readouterr().err


def test_flags_override_config(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import any_mcp._fastmcp_adapter as adapter

    seen: list[HttpSettings] = []

    async def fake_run_http(server: Server, settings: HttpSettings) -> None:
        seen.append(settings)

    monkeypatch.setattr(adapter, "run_http", fake_run_http)
    path = tmp_path / "c.yaml"
    path.write_text(
        f"python_path: ['{ROOT}']\n"
        "server:\n  transport: http\n  http: {host: 127.0.0.2, port: 9001, path: /cfg}\n"
        "plugins:\n  - name: p\n    plugin: 'tests.fixtures.stdio_plugin:StdioPlugin'\n",
        encoding="utf-8",
    )
    assert cli.main(["--config", str(path)]) == 0
    assert (seen[-1].host, seen[-1].port, seen[-1].path) == ("127.0.0.2", 9001, "/cfg")
    argv = ["--config", str(path), "--host", "127.0.0.1", "--port", "9100", "--path", "/x"]
    assert cli.main(argv) == 0
    assert (seen[-1].host, seen[-1].port, seen[-1].path) == ("127.0.0.1", 9100, "/x")
    assert cli.main(["--config", str(path), "--transport", "stdio", "--list-tools"]) == 0
