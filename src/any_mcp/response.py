"""Default shaping of an upstream response into a size-bounded ToolResult (design D9)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from any_mcp.types import NO_VALUE, RawResponse, ToolResult

DEFAULT_BUDGET_CHARS = 40000


def _is_json(raw: RawResponse) -> bool:
    if raw.value is not NO_VALUE:
        return not isinstance(raw.value, (str, bytes))
    mime = raw.content_type.split(";", 1)[0].strip().lower()
    return mime == "application/json" or mime.endswith("+json")


def _describe(raw: RawResponse) -> tuple[str, Any]:
    """Return (text, structured) before the budget is applied."""
    if not raw.body:
        return f"OK ({raw.status})", None
    if _is_json(raw):
        try:
            value = raw.json()
        except ValueError:
            return raw.text(), None
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return text, value
    mime = raw.content_type.split(";", 1)[0].strip().lower()
    if mime.startswith("text/"):
        return raw.text(), None
    return f"{len(raw.body)} bytes of {raw.content_type}", None


def shape_default(
    raw: RawResponse,
    *,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    output_schema: Mapping[str, Any] | None = None,
) -> ToolResult:
    """Turn a RawResponse into a ToolResult, truncating text beyond ``budget_chars``."""
    text, structured = _describe(raw)
    total = len(text)
    if total <= budget_chars:
        return ToolResult(text=text, structured=structured)
    if output_schema is not None:
        return ToolResult(
            text=(
                f"response of {total} chars exceeds the {budget_chars} char budget and cannot "
                "be truncated because the tool declares an output schema"
            ),
            is_error=True,
        )
    return ToolResult(text=f"{text[:budget_chars]}\n[truncated: {budget_chars} of {total} chars]")
