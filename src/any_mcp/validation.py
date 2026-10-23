"""Validation of call arguments against the original tool input schema."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from any_mcp.errors import ToolInputError
from any_mcp.schema import SchemaError, _resolve_pointer

_COERCIBLE = {"object": dict, "array": list}
_MAX_BRANCH_DEPTH = 8
_MAX_VALUE_DEPTH = 32


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _resolve(sub: Any, root: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    while isinstance(sub, dict) and isinstance(sub.get("$ref"), str):
        ref = sub["$ref"]
        if ref in seen:
            return {}
        seen = (*seen, ref)
        try:
            sub = _resolve_pointer(ref, root)
        except SchemaError:
            return {}
    return sub


def _variants(sub: Any, root: dict[str, Any], depth: int = 0) -> list[dict[str, Any]]:
    """The schema plus every anyOf/oneOf/allOf branch, with local refs resolved."""
    sub = _resolve(sub, root)
    if not isinstance(sub, dict) or depth > _MAX_BRANCH_DEPTH:
        return []
    out = [sub]
    for key in ("anyOf", "oneOf", "allOf"):
        branches = sub.get(key)
        if isinstance(branches, list):
            for branch in branches:
                out.extend(_variants(branch, root, depth + 1))
    return out


def _coerce_value(value: Any, sub: Any, root: dict[str, Any], depth: int = 0) -> Any:
    if depth > _MAX_VALUE_DEPTH:
        return value
    variants = _variants(sub, root)
    if isinstance(value, str):
        types: set[str] = set()
        for variant in variants:
            declared = variant.get("type")
            types.update(declared if isinstance(declared, list) else [declared])
        if "string" in types:
            return value
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if not any(isinstance(parsed, _COERCIBLE[t]) for t in types if t in _COERCIBLE):
            return value
        return _coerce_value(parsed, sub, root, depth + 1)
    if isinstance(value, dict):
        out = dict(value)
        for variant in variants:
            props = variant.get("properties")
            if isinstance(props, dict):
                for name in value:
                    if name in props:
                        out[name] = _coerce_value(out[name], props[name], root, depth + 1)
        return out
    if isinstance(value, list):
        out_list = list(value)
        for variant in variants:
            prefix = variant.get("prefixItems")
            items = variant.get("items")
            for i, item in enumerate(out_list):
                if isinstance(prefix, list) and i < len(prefix):
                    out_list[i] = _coerce_value(item, prefix[i], root, depth + 1)
                elif isinstance(items, dict):
                    out_list[i] = _coerce_value(item, items, root, depth + 1)
        return out_list
    return value


def _coerce(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    out = _coerce_value(arguments, schema, schema)
    return out if isinstance(out, dict) else arguments


def _message(error: Any) -> str:
    """The error message with the offending argument value masked, so it never leaks."""
    message = str(error.message)
    shown = repr(error.instance)
    return "<value>" + message[len(shown) :] if message.startswith(shown) else message


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Validate ``arguments`` (None means ``{}``); return them, with JSON strings coerced."""
    coerced = _coerce(schema, arguments if arguments is not None else {})
    validator = Draft202012Validator(schema)
    errors = sorted(
        {(_json_path(e.absolute_path), _message(e)) for e in validator.iter_errors(coerced)}
    )
    if errors:
        raise ToolInputError(errors)
    return coerced
