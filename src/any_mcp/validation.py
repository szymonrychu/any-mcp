"""Validation of call arguments against the original tool input schema."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from any_mcp.errors import ToolInputError

_COERCIBLE = {"object": dict, "array": list}


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _coerce(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return arguments
    out = dict(arguments)
    for name, value in arguments.items():
        sub = properties.get(name)
        if not isinstance(value, str) or not isinstance(sub, dict):
            continue
        declared = sub.get("type")
        types = declared if isinstance(declared, list) else [declared]
        if "string" in types:
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        if any(isinstance(parsed, _COERCIBLE[t]) for t in types if t in _COERCIBLE):
            out[name] = parsed
    return out


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Validate ``arguments`` (None means ``{}``); return them, with JSON strings coerced."""
    coerced = _coerce(schema, arguments if arguments is not None else {})
    validator = Draft202012Validator(schema)
    errors = sorted(
        {(_json_path(e.absolute_path), e.message) for e in validator.iter_errors(coerced)}
    )
    if errors:
        raise ToolInputError(errors)
    return coerced
