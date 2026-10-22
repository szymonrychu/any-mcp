"""Execute a declarative HttpBinding with httpx. Only the ToolCall arguments and the explicit
``headers`` parameter reach the request; nothing from an inbound MCP request is an input."""

from __future__ import annotations

import re
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import quote

import httpx

from any_mcp.errors import ConfigError, ToolInputError, UpstreamError
from any_mcp.types import HttpBinding, RawResponse, ToolCall

_PATH_PARAM = re.compile(r"\{([^{}]+)\}")


def _package_version() -> str:
    try:
        return version("any-mcp")
    except PackageNotFoundError:
        return "0"


def make_client(
    base_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    """An httpx client with the any-mcp user agent and no credentials of its own."""
    return httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        timeout=timeout,
        headers={"User-Agent": f"any-mcp/{_package_version()}"},
        follow_redirects=False,
        trust_env=False,
    )


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _fill_path(path: str, arguments: Mapping[str, Any]) -> tuple[str, set[str]]:
    used: set[str] = set()
    missing: list[tuple[str, str]] = []

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        value = arguments.get(name)
        if value is None:
            missing.append((name, f"path parameter {name!r} is required"))
            return ""
        used.add(name)
        return quote(_text(value), safe="")

    filled = _PATH_PARAM.sub(fill, path)
    if missing:
        raise ToolInputError(missing)
    return filled, used


def _query_items(binding: HttpBinding, arguments: Mapping[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for name in binding.query:
        value = arguments.get(name)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        items.extend((name, _text(v)) for v in values if v is not None)
    return items


def _body(binding: HttpBinding, arguments: Mapping[str, Any], consumed: set[str]) -> Any:
    mode = binding.body
    if mode is None:
        return None
    if mode == "*":
        return {k: v for k, v in arguments.items() if k not in consumed}
    if isinstance(mode, str):
        return arguments.get(mode)
    return {k: arguments[k] for k in mode if k in arguments}


def _url(client: httpx.AsyncClient, binding: HttpBinding, path: str) -> str:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path):
        return path
    if binding.base_url:
        return binding.base_url.rstrip("/") + "/" + path.lstrip("/")
    if not str(client.base_url):
        raise ConfigError(f"relative path {binding.path!r} has no base URL")
    return path


async def execute_binding(
    client: httpx.AsyncClient,
    call: ToolCall,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> RawResponse:
    """Send the request described by ``call.spec.http`` and return the response."""
    binding = call.spec.http
    if binding is None:
        raise ConfigError(f"tool {call.tool!r} has no http binding")
    arguments = call.arguments
    path, consumed = _fill_path(binding.path, arguments)
    consumed |= set(binding.query) | set(binding.headers)
    url = _url(client, binding, path)

    request_headers: dict[str, str] = {}
    for arg, header in binding.headers.items():
        value = arguments.get(arg)
        if value is not None:
            request_headers[header] = _text(value)
    request_headers.update(headers or {})

    kwargs: dict[str, Any] = {}
    body = _body(binding, arguments, consumed)
    if binding.body is not None:
        kwargs["json"] = body
    request = client.build_request(
        binding.method.upper(),
        url,
        params=_query_items(binding, arguments) or None,
        headers=request_headers,
        timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
        **kwargs,
    )
    response = await client.send(request)
    raw = RawResponse(
        response.status_code,
        dict(response.headers),
        response.content,
        response.headers.get("content-type", ""),
    )
    if raw.status >= 400:
        raise UpstreamError(raw)
    return raw
