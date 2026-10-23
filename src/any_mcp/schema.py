"""Tool schema normalisation for listing, and client-safe tool names."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from any_mcp.errors import ConfigError

_TRUNCATED: dict[str, Any] = {"type": "object", "description": "(recursive, truncated)"}
_DEFINITION_KEYS = ("$defs", "definitions", "components")
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class SchemaError(ConfigError):
    """A tool schema cannot be normalised."""


def _resolve_pointer(ref: str, root: dict[str, Any]) -> Any:
    if not ref.startswith("#"):
        raise SchemaError(f"unsupported external $ref: {ref}")
    node: Any = root
    if ref != "#" and not ref.startswith("#/"):
        raise SchemaError(f"unresolvable $ref: {ref}")
    parts = [] if ref == "#" else ref[2:].split("/")
    for raw in parts:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise SchemaError(f"unresolvable $ref: {ref}")
    return node


def _inline(node: Any, root: dict[str, Any], stack: tuple[str, ...], max_depth: int) -> Any:
    if isinstance(node, list):
        return [_inline(item, root, stack, max_depth) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if len(stack) >= max_depth:
            return dict(_TRUNCATED)
        target = _resolve_pointer(ref, root)
        if not isinstance(target, dict):
            raise SchemaError(f"unresolvable $ref: {ref}")
        if target is root:
            target = {k: v for k, v in target.items() if k not in _DEFINITION_KEYS}
        resolved = _inline(target, root, (*stack, ref), max_depth)
        siblings = {k: _inline(v, root, stack, max_depth) for k, v in node.items() if k != "$ref"}
        return {**resolved, **siblings}
    return {k: _inline(v, root, stack, max_depth) for k, v in node.items()}


def normalize_schema(
    schema: dict[str, Any], *, max_depth: int = 8, root: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Inline local ``$ref``s (resolved against ``root``, default the schema itself).

    ``$defs``/``definitions`` are dropped from the result; a reference recursing deeper
    than ``max_depth`` becomes a placeholder object. External refs raise SchemaError.
    """
    document = root if root is not None else schema
    out = _inline(schema, document, (), max_depth)
    if root is None:
        out.pop("components", None)
    out.pop("$defs", None)
    out.pop("definitions", None)
    return out


def _is_object_schema(branch: Any) -> bool:
    return isinstance(branch, dict) and branch.get("type") == "object"


def listing_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Shape a schema for tools/list: the root must be a single object schema."""
    if schema is None:
        return {"type": "object", "additionalProperties": False}
    if schema.get("type") == "object":
        return schema
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches and all(_is_object_schema(b) for b in branches):
            return _merge_objects(schema, key, branches)
    raise SchemaError(
        f"tool input schema root must be an object schema, got {schema.get('type')!r}"
    )


def _merge_objects(
    schema: dict[str, Any], key: str, branches: list[dict[str, Any]]
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for branch in branches:
        for name, prop in branch.get("properties", {}).items():
            properties.setdefault(name, copy.deepcopy(prop))
    common = set.intersection(*(set(b.get("required", [])) for b in branches))
    ordered = [n for n in branches[0].get("required", []) if n in common]
    out = {k: v for k, v in schema.items() if k != key}
    out.update(type="object", properties=properties)
    if ordered:
        out["required"] = ordered
    return out


def exposed_name(namespace: str, tool: str, *, max_length: int = 60) -> str:
    """Client-safe tool name: ``<namespace>_<tool>``, sanitised and hash-truncated."""
    name = _NAME_UNSAFE.sub("_", f"{namespace}_{tool}" if namespace else tool)
    if len(name) <= max_length:
        return name
    suffix = "_" + hashlib.sha1(name.encode()).hexdigest()[:6]  # noqa: S324
    return name[: max_length - len(suffix)] + suffix
