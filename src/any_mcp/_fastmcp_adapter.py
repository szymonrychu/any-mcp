"""Serve a ``ToolRegistry`` over MCP with FastMCP 4.x.

This is the only module in any-mcp that imports ``fastmcp``, ``mcp`` or ``mcp_types``
(design D1, enforced by ``tests/test_import_boundary.py``). Nothing in its public
signatures is a FastMCP type: ``Server`` is opaque and ``InMemoryClient`` speaks plain
dicts, lists and strings.

Probe results (fastmcp 4.1.3 on mcp 2.3.0, pinned by ``tests/test_adapter.py``):

- Tool subclass API. ``fastmcp.tools.base.Tool`` takes a raw JSON-schema ``parameters``
  dict plus ``output_schema``, ``title`` and ``annotations``; a subclass implements
  ``async run(arguments) -> ToolResult``. A custom ``fastmcp.server.providers.Provider``
  overriding ``_list_tools``/``_get_tool`` serves them, so FastMCP asks the registry on
  every ``tools/list`` and ``tools/call`` and a reload is visible on the next request.
  ``Tool.to_mcp_tool`` invents a title from the name when there is none; ``_RegistryTool``
  passes its own (possibly absent) title instead. FastMCP's ``ToolResult`` rejects a
  non-object ``structuredContent``, which MCP allows, so results are handed over as a
  prebuilt ``CallToolResult`` that FastMCP returns verbatim.
- Validation bypass. Only ``FunctionTool`` validates arguments (against its pydantic
  model); the base ``Tool`` passes ``arguments`` to ``run()`` untouched, so the any-mcp
  pipeline is the only validator and invalid arguments come back as ``isError`` results.
  ``strict_input_validation=False`` is set anyway and ``dereference_schemas=False`` keeps
  the registry's already-normalised schemas byte for byte.
- Unknown tools. FastMCP answers an unknown ``tools/call`` with an ``isError`` result;
  ``_UnknownToolGuard`` middleware raises ``MCPError(-32602)`` first, which FastMCP lets
  through to the wire as a JSON-RPC error.
- Caching. FastMCP's ``cache_ttl`` is whole seconds and rejects 0, so the adapter sets the
  SDK ``CacheHint`` on the low-level server in milliseconds: ``ttlMs`` equals
  ``tools.list_ttl_ms`` exactly. The SDK only stamps it on 2026-07-28 results.
- Capabilities. FastMCP registers resources, prompts and logging handlers and the SDK
  derives capabilities from registered handlers, so the adapter removes every handler but
  tools, discovery, ping and ``subscriptions/listen`` (those methods answer -32601) and
  declares ``{"tools": {"listChanged": true}}`` alone in both eras.
- Notifications. FastMCP only notifies inside a request (``ctx.send_notification``) and
  serves no ``subscriptions/listen``; a 2026-07-28 connection has no standing channel, so
  out-of-request notifications on it are dropped. The adapter therefore sends one
  notification per real registry change on two paths, one per era:
  2026-07-28: an SDK ``ListenHandler`` on an ``InMemorySubscriptionBus`` serves
  ``subscriptions/listen``; ``bus.publish(ToolsListChanged())`` reaches every stream that
  asked for ``toolsListChanged`` (a client that opened none gets nothing, per the spec).
  Handshake era: SDK middleware records each legacy ``Connection`` (``session._connection``,
  the same reach FastMCP's own ping middleware and stateful proxy use) until its exit stack
  closes; ``Connection.send_tool_list_changed()`` writes ``notifications/tools/list_changed``
  on its standalone channel. Correctness never depends on either (design D8).
- Input required (MRTR, design D19). Probe: ``mcp_types.InputRequiredResult`` exists and
  FastMCP 4.1.3 carries it as a tool return: ``Tool.run`` may return
  ``fastmcp.tools.base.InputRequiredToolResult`` (a ``ToolResult`` subclass that wraps the
  result), which flows through the middleware chain and which the wire handler unwraps into
  the ``InputRequiredResult`` response. A retry's ``inputResponses`` and ``requestState``
  are read from ``Context.input_responses`` / ``Context.request_state``. The SDK seals
  ``requestState`` on the wire (per-process key) and unseals it before the tool runs, so
  plugins see the plaintext they put in ``InputRequired.state``. No fallback to
  ``MCPServer`` was needed. Responses reach plugins as plain JSON dicts
  (``{"action": ..., "content": ...}`` for an elicitation). A handshake-era request cannot
  retry, so it gets an ``isError`` result saying the tool needs a 2026-07-28 client.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastmcp import Client, FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers import Provider
from fastmcp.tools.base import InputRequiredToolResult, Tool
from fastmcp.tools.base import ToolResult as FastMCPToolResult
from mcp.client.subscriptions import listen
from mcp.server.caching import CacheHint
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
    Request,
    ServerCapabilities,
    SubscriptionsListenRequestParams,
    TextContent,
    ToolListChangedNotification,
)
from mcp_types.methods import MONOLITH_REQUESTS
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, PrivateAttr, TypeAdapter
from starlette.middleware import Middleware as AsgiMiddleware
from starlette.responses import PlainTextResponse

from any_mcp import __version__
from any_mcp.config import AnyMcpConfig, HttpSettings
from any_mcp.errors import ConfigError, UnknownTool
from any_mcp.pipeline import call_tool
from any_mcp.registry import ExposedTool, ToolRegistry
from any_mcp.types import InputRequired, ToolResult, _thaw

__all__ = [
    "InMemoryClient",
    "ProtocolError",
    "Server",
    "build_http_app",
    "build_server",
    "in_memory_client",
    "run_http",
    "run_stdio",
]

TOOL_LIST_CHANGED = "notifications/tools/list_changed"
_SERVED = frozenset({"tools/list", "tools/call", "server/discover", "ping", "subscriptions/listen"})
_CACHED = ("tools/list", "server/discover")
_RAW = TypeAdapter(dict[str, Any])


class ProtocolError(Exception):
    """A JSON-RPC error answer: ``code`` (e.g. -32602) and ``message``."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(by_alias=True, mode="json", exclude_unset=True, exclude_none=True)


# -- tools -----------------------------------------------------------------------------------


def _modern_request() -> bool:
    """Whether the current request speaks 2026-07-28 (True outside any request)."""
    try:
        return get_context().session.protocol_version in MODERN_PROTOCOL_VERSIONS
    except RuntimeError:
        return True


def _wire_result(result: ToolResult, *, modern: bool) -> FastMCPToolResult:
    """An any-mcp ToolResult as a FastMCP result that FastMCP sends unchanged.

    Before 2026-07-28 ``structuredContent`` must be an object, so a non-object value is
    sent only as its JSON text there.
    """
    structured = result.structured if isinstance(result.structured, dict) else None
    wire = CallToolResult(
        content=[TextContent(type="text", text=result.text)],
        structured_content=result.structured if modern else structured,
        is_error=result.is_error,
    )
    shaped = FastMCPToolResult.model_construct(
        content=wire.content, structured_content=structured, meta=None, is_error=result.is_error
    )
    shaped._raw_mcp_result = wire
    return shaped


_NEEDS_MODERN = (
    "this tool needs to ask for input, which requires a client that supports MCP "
    "2026-07-28 (input required); this client negotiated an older protocol version"
)


def _input_required_result(needed: InputRequired) -> InputRequiredToolResult:
    """The plugin's ``InputRequired`` as the 2026-07-28 ``InputRequiredResult``."""
    requests = {
        key: ElicitRequest(
            params=ElicitRequestFormParams(
                message=request.message, requested_schema=_thaw(request.schema)
            )
        )
        for key, request in needed.requests.items()
    }
    return InputRequiredToolResult(
        InputRequiredResult(input_requests=requests, request_state=needed.state)
    )


def _retry_fields() -> tuple[dict[str, Any], str | None]:
    """``inputResponses`` (as plain JSON) and ``requestState`` of the current retry."""
    try:
        ctx = get_context()
    except RuntimeError:
        return {}, None
    responses = {key: _dump(value) for key, value in (ctx.input_responses or {}).items()}
    return responses, ctx.request_state


class _RegistryTool(Tool):
    """One exposed registry tool; ``run`` goes through the any-mcp call pipeline."""

    _registry: ToolRegistry = PrivateAttr()

    def to_mcp_tool(self, **overrides: Any) -> Any:
        return super().to_mcp_tool(**{"title": self.title, **overrides})

    async def run(self, arguments: dict[str, Any]) -> FastMCPToolResult:
        modern = _modern_request()
        responses, state = _retry_fields() if modern else ({}, None)
        try:
            result = await call_tool(
                self._registry,
                self.name,
                arguments,
                input_responses=responses,
                request_state=state,
            )
        except UnknownTool as exc:
            # removed by a reload between the guard and this call
            result = ToolResult(text=str(exc), is_error=True)
        if result.input_required is not None:
            if modern:
                return _input_required_result(result.input_required)
            return _wire_result(ToolResult(text=_NEEDS_MODERN, is_error=True), modern=False)
        return _wire_result(result, modern=modern)


def _to_fastmcp(registry: ToolRegistry, exposed: ExposedTool) -> _RegistryTool:
    spec = exposed.spec
    tool = _RegistryTool(
        name=exposed.name,
        title=spec.title,
        description=spec.description,
        parameters=exposed.listed_schema,
        output_schema=exposed.output_schema,
        annotations=_thaw(spec.annotations) if spec.annotations is not None else None,
        tags=set(spec.tags),
    )
    tool._registry = registry
    return tool


class _RegistryProvider(Provider):
    """Lists ``registry.tools()`` (sorted by the registry) on every request."""

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__()
        self._registry = registry
        self._source: tuple[ExposedTool, ...] = ()
        self._tools: dict[str, _RegistryTool] = {}

    def _current(self) -> dict[str, _RegistryTool]:
        source = tuple(self._registry.tools())
        if len(source) != len(self._source) or any(
            a is not b for a, b in zip(source, self._source, strict=True)
        ):
            self._tools = {t.name: _to_fastmcp(self._registry, t) for t in source}
            self._source = source
        return self._tools

    async def _list_tools(self) -> Sequence[Tool]:
        return list(self._current().values())

    async def _get_tool(self, name: str, version: Any = None) -> Tool | None:
        return self._current().get(name)


class _UnknownToolGuard(Middleware):
    """Answer a call to a tool the registry does not have with JSON-RPC -32602."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def on_call_tool(
        self, context: MiddlewareContext[CallToolRequestParams], call_next: CallNext[Any, Any]
    ) -> Any:
        name = context.message.name
        if self._registry.get(name) is None:
            raise MCPError(INVALID_PARAMS, f"unknown tool: {name}")
        return await call_next(context)


# -- server ----------------------------------------------------------------------------------


class Server:
    """An any-mcp MCP server: the registry, its config and the FastMCP instance serving it."""

    def __init__(
        self,
        registry: ToolRegistry,
        config: AnyMcpConfig,
        aclose: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.registry = registry
        self._aclose = aclose
        self.config = config
        self._bus = InMemorySubscriptionBus()
        self._listen = ListenHandler(self._bus)
        self._legacy: dict[int, Any] = {}
        self._fastmcp = FastMCP(
            config.server.name,
            config.server.instructions,
            version=__version__,
            providers=[_RegistryProvider(registry)],
            middleware=[_UnknownToolGuard(registry)],
            lifespan=self._lifespan,
            strict_input_validation=False,
            mask_error_details=True,
            dereference_schemas=False,
        )
        low = self._fastmcp._mcp_server
        hint = CacheHint(ttl_ms=config.tools.list_ttl_ms, scope=config.tools.cache_scope)
        low.cache_hints = dict.fromkeys(_CACHED, hint)
        low.add_request_handler(
            "subscriptions/listen", SubscriptionsListenRequestParams, self._listen
        )
        for method in [m for m in low._request_handlers if m not in _SERVED]:
            del low._request_handlers[method]
        derive = low.get_capabilities

        def tools_only(*args: Any, **kwargs: Any) -> ServerCapabilities:
            return ServerCapabilities(tools=derive(*args, **kwargs).tools)

        low.get_capabilities = tools_only
        low.middleware.insert(0, self._track_legacy)
        registry.add_listener(self._tools_changed)

    async def aclose(self) -> None:
        """Release what the server's owner handed over (the plugin HTTP clients), once, at
        final shutdown. The lifespan never calls this: it can run more than once."""
        if self._aclose is not None:
            await self._aclose()

    @asynccontextmanager
    async def _lifespan(self, _app: Any) -> AsyncIterator[dict[str, Any]]:
        await self.registry.start()
        periodic = asyncio.create_task(self.registry.run_periodic())
        try:
            yield {}
        finally:
            self._listen.close()
            periodic.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await periodic
            await self.registry.stop()

    async def _track_legacy(self, ctx: Any, call_next: Any) -> Any:
        """Remember each handshake-era connection so a reload can notify it."""
        if ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            connection = getattr(ctx.session, "_connection", None)
            key = id(connection)
            if (
                connection is not None
                and connection.has_standalone_channel
                and key not in self._legacy
            ):
                self._legacy[key] = connection
                connection.exit_stack.callback(self._legacy.pop, key, None)
        return await call_next(ctx)

    async def _tools_changed(self) -> None:
        await self._bus.publish(ToolsListChanged())
        for connection in list(self._legacy.values()):
            await connection.send_tool_list_changed()


def build_server(
    registry: ToolRegistry,
    config: AnyMcpConfig,
    *,
    aclose: Callable[[], Awaitable[None]] | None = None,
) -> Server:
    """Wrap ``registry`` in a FastMCP server configured from ``config``.

    ``aclose`` releases the owner's resources; ``run_stdio`` and ``run_http`` call it once
    serving ends for good.
    """
    return Server(registry, config, aclose)


async def run_stdio(server: Server) -> None:
    """Serve ``server`` over stdio until stdin closes; stdout carries only MCP messages."""
    try:
        await server._fastmcp.run_stdio_async(show_banner=False)
    finally:
        await server.aclose()


# -- streamable HTTP -------------------------------------------------------------------------

_log = logging.getLogger("any_mcp.http")
_STRIPPED_HEADERS = (b"authorization", b"cookie")


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class _HttpGuard:
    """Pure ASGI guard in front of the MCP endpoint.

    A request with an ``Origin`` that is neither loopback nor allow-listed gets 403 (the MCP
    transport rule against DNS rebinding). With a bearer token configured, a request without
    exactly that token gets 401. A passing request is stripped of ``Authorization`` and
    ``Cookie`` before FastMCP sees it: inbound credentials are not forwardable by anything.
    """

    def __init__(self, app: Any, *, token: str | None, allowed_origins: Sequence[str]) -> None:
        self.app = app
        self.token = token.encode() if token is not None else None
        self.allowed = {o.strip().rstrip("/").lower() for o in allowed_origins}

    def _origin_ok(self, origin: str) -> bool:
        if origin.strip().rstrip("/").lower() in self.allowed:
            return True
        try:
            return _is_loopback_host(urlsplit(origin).hostname)
        except ValueError:
            return False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        origin = headers.get(b"origin")
        if origin is not None and not self._origin_ok(origin.decode("latin-1")):
            await PlainTextResponse("forbidden origin", status_code=403)(scope, receive, send)
            return
        if self.token is not None:
            auth = headers.get(b"authorization", b"")
            scheme, _, value = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(value.strip(), self.token):
                response = PlainTextResponse(
                    "unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        scope = {
            **scope,
            "headers": [(k, v) for k, v in scope["headers"] if k not in _STRIPPED_HEADERS],
        }
        await self.app(scope, receive, send)


def check_http_exposure(settings: HttpSettings) -> None:
    """Refuse a non-loopback bind without a bearer token unless ``insecure`` is set."""
    if (
        settings.bearer_token is None
        and not settings.insecure
        and not _is_loopback_host(settings.host)
    ):
        raise ConfigError(
            f"refusing to listen on {settings.host} without server.http.bearer_token: anyone "
            "who can reach it could call every tool; set a token, bind a loopback host, or "
            "pass --insecure (server.http.insecure) to accept the risk"
        )


def build_http_app(server: Server, settings: HttpSettings) -> Any:
    """The ASGI app serving ``server`` as stateless streamable HTTP at ``settings.path``."""
    check_http_exposure(settings)
    token = settings.bearer_token.get_secret_value() if settings.bearer_token is not None else None
    if token is None and not _is_loopback_host(settings.host):
        _log.warning(
            "listening on %s without server.http.bearer_token (--insecure): anyone who can "
            "reach it can call every tool",
            settings.host,
        )
    guard = AsgiMiddleware(_HttpGuard, token=token, allowed_origins=settings.allowed_origins)
    return server._fastmcp.http_app(
        path=settings.path,
        middleware=[guard],
        stateless_http=True,
        host_origin_protection=False,  # the guard owns the Origin rule, loopback or not
    )


async def run_http(server: Server, settings: HttpSettings) -> None:
    """Serve ``server`` over streamable HTTP on ``settings.host:port`` until cancelled."""
    import uvicorn

    app = build_http_app(server, settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        lifespan="on",
        log_config=None,
        timeout_graceful_shutdown=2,
    )
    try:
        await uvicorn.Server(config).serve()
    finally:
        await server.aclose()


# -- in-memory client ------------------------------------------------------------------------


class InMemoryClient:
    """A connected in-process MCP client speaking plain Python types."""

    def __init__(self, client: Client[Any], *, legacy: bool) -> None:
        self._client = client
        self._legacy = legacy
        self._notifications: list[str] = []

    @property
    def protocol_version(self) -> str:
        return str(self._client.protocol_version)

    @property
    def instructions(self) -> str | None:
        return self._client.instructions

    @property
    def server_name(self) -> str | None:
        info = self._client.server_info
        return info.name if info is not None else None

    def capabilities(self) -> dict[str, Any]:
        """The server capabilities as negotiated (handshake or ``server/discover``)."""
        caps = self._client.server_capabilities
        return _dump(caps) if caps is not None else {}

    def notifications(self) -> list[str]:
        """Methods of the tool-list-changed notifications received so far, in order."""
        return list(self._notifications)

    async def list_tools_result(self) -> dict[str, Any]:
        """The raw ``tools/list`` result, with ``ttlMs``/``cacheScope`` where the era has them."""
        try:
            return _dump(await self._client.list_tools_mcp(cache_mode="bypass"))
        except MCPError as exc:
            raise ProtocolError(exc.error.code, exc.error.message) from None

    async def list_tools(self) -> list[dict[str, Any]]:
        """Every listed tool as an MCP tool dict."""
        return list((await self.list_tools_result())["tools"])

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """``{"text", "structured", "is_error"}``; a JSON-RPC error raises ProtocolError."""
        try:
            result = await self._client.call_tool_mcp(name, dict(arguments or {}))
        except MCPError as exc:
            raise ProtocolError(exc.error.code, exc.error.message) from None
        text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
        return {"text": text, "structured": result.structured_content, "is_error": result.is_error}

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Send any request and return its raw result; a JSON-RPC error raises ProtocolError."""
        body = {"method": method, "params": dict(params) if params is not None else None}
        model = MONOLITH_REQUESTS.get(method, Request)
        try:
            return await self._client.session.send_request(model.model_validate(body), _RAW)
        except MCPError as exc:
            raise ProtocolError(exc.error.code, exc.error.message) from None

    async def _on_message(self, message: Any) -> None:
        # 2026-07-28 listen frames reach the message handler too; they are counted once,
        # from the subscription stream, so only handshake-era notifications count here.
        if self._legacy and isinstance(message, ToolListChangedNotification):
            self._notifications.append(TOOL_LIST_CHANGED)

    async def _drain(self, subscription: AsyncIterator[Any]) -> None:
        async for event in subscription:
            if isinstance(event, ToolsListChanged):
                self._notifications.append(TOOL_LIST_CHANGED)


@asynccontextmanager
async def in_memory_client(
    server: Server, *, legacy: bool = False, subscribe: bool = False
) -> AsyncIterator[InMemoryClient]:
    """Connect an in-process client to ``server``, running its lifespan.

    ``legacy`` uses the initialize handshake instead of the 2026-07-28 protocol.
    ``subscribe`` (2026-07-28 only) opens a ``subscriptions/listen`` stream for tool-list
    changes; a legacy client always receives them on its connection.
    """
    if legacy and subscribe:
        raise ValueError("subscribe needs the 2026-07-28 protocol; legacy clients always listen")
    holder: list[InMemoryClient] = []

    async def on_message(message: Any) -> None:
        if holder:
            await holder[0]._on_message(message)

    client = Client(
        server._fastmcp, mode="legacy" if legacy else "auto", message_handler=on_message
    )
    async with client:
        wrapper = InMemoryClient(client, legacy=legacy)
        holder.append(wrapper)
        if not subscribe:
            yield wrapper
            return
        # entering returns once the server acknowledged the subscription
        async with listen(client.session, tools_list_changed=True) as subscription:
            task = asyncio.create_task(wrapper._drain(subscription))
            try:
                yield wrapper
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
