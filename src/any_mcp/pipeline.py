"""One tool call: validate, run the plugin hooks, and turn every failure into an error result."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from any_mcp.errors import CallTimeout, UnknownTool, error_result
from any_mcp.registry import ExposedTool, ToolRegistry
from any_mcp.types import ToolCall, ToolResult
from any_mcp.validation import validate_arguments

log = logging.getLogger("any_mcp.pipeline")


def _outcome(result: ToolResult) -> str:
    if not result.is_error:
        return "ok"
    structured = result.structured
    error = structured.get("error") if isinstance(structured, Mapping) else None
    kind = error.get("kind") if isinstance(error, Mapping) else None
    return kind if isinstance(kind, str) and kind else "error"


async def _run(tool: ExposedTool, call: ToolCall, arguments: Mapping[str, Any]) -> ToolResult:
    loaded = tool.plugin
    plugin = loaded.instance
    validated = validate_arguments(tool.input_schema, dict(arguments))
    call = call.with_arguments(validated)
    call = await plugin.before_call(call)
    try:
        async with asyncio.timeout(loaded.timeout):
            raw = await plugin.call(call)
            return await plugin.shape_response(call, raw)
    except TimeoutError as exc:
        if isinstance(exc, CallTimeout):
            raise
        raise CallTimeout(loaded.timeout) from exc


async def call_tool(
    registry: ToolRegistry,
    name: str,
    arguments: Mapping[str, Any] | None,
    *,
    request_id: str | None = None,
) -> ToolResult:
    """Run one call. Raises ``UnknownTool``; only cancellation escapes otherwise."""
    tool = registry.get(name)
    if tool is None:
        raise UnknownTool(f"unknown tool: {name}")
    loaded = tool.plugin
    plugin_name = loaded.entry.name
    request_id = request_id or uuid4().hex[:12]
    args = dict(arguments) if arguments is not None else {}
    call = ToolCall(
        plugin=plugin_name,
        tool=tool.spec.name,
        exposed_name=tool.name,
        arguments=args,
        spec=tool.spec,
        request_id=request_id,
    )
    started = time.monotonic()
    try:
        result = await _run(tool, call, args)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            result = await loaded.instance.on_error(call, exc)
        except Exception:
            result = error_result(exc, plugin=plugin_name)
    log.info(
        "call req=%s tool=%s duration_ms=%d outcome=%s",
        request_id,
        tool.name,
        round((time.monotonic() - started) * 1000),
        _outcome(result),
    )
    return result
