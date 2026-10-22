"""Plain data types shared by the core and plugins. No fastmcp or mcp imports."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel


class _NoValue:
    """Sentinel type: a RawResponse that does not wrap a handler value."""

    _instance: _NoValue | None = None

    def __new__(cls) -> _NoValue:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NO_VALUE"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _NoValue:
        return self

    def __deepcopy__(self, memo: Any) -> _NoValue:
        return self


NO_VALUE: Any = _NoValue()

_EMPTY_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {"type": "object", "additionalProperties": False}
)


def _freeze(value: Any) -> Any:
    """Recursively turn dicts into read-only mappings and lists into tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    """Inverse of _freeze: plain dicts and lists, safe to serialise or mutate."""
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_thaw(value), sort_keys=True, default=repr)


def _set(obj: Any, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


@dataclass(frozen=True)
class HttpBinding:
    """Declarative description of one upstream HTTP endpoint.

    ``headers`` maps argument name to header name. ``body``: None for no body, ``"*"`` for every
    argument not used by path, query or headers as a JSON object, one argument name for that
    argument's value as the JSON body, a tuple of names for those arguments as a JSON object.
    """

    method: str
    path: str
    query: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str | tuple[str, ...] | None = None
    base_url: str | None = None

    def __post_init__(self) -> None:
        _set(self, "query", tuple(self.query))
        _set(self, "headers", MappingProxyType(dict(self.headers)))
        if isinstance(self.body, list):
            _set(self, "body", tuple(self.body))

    def _key(self) -> tuple[Any, ...]:
        return (
            self.method,
            self.path,
            self.query,
            _canonical(self.headers),
            self.body,
            self.base_url,
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, HttpBinding) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())


@dataclass(frozen=True)
class ToolSpec:
    """One tool a plugin offers."""

    name: str
    description: str
    input_schema: Mapping[str, Any] | None = None
    input_model: type[BaseModel] | None = None
    output_schema: Mapping[str, Any] | None = None
    title: str | None = None
    tags: frozenset[str] = frozenset()
    annotations: Mapping[str, Any] | None = None
    http: HttpBinding | None = None
    handler: Callable[[ToolCall], Awaitable[Any]] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("ToolSpec.name must not be empty")
        if not self.description or not self.description.strip():
            raise ValueError(f"ToolSpec {self.name!r}: description must not be empty")
        if self.input_schema is not None and self.input_model is not None:
            raise ValueError(f"ToolSpec {self.name!r}: give input_schema or input_model, not both")
        if self.http is not None and self.handler is not None:
            raise ValueError(f"ToolSpec {self.name!r}: give http or handler, not both")
        _set(self, "tags", frozenset(self.tags))
        for attr in ("input_schema", "output_schema", "annotations"):
            current = getattr(self, attr)
            if current is not None:
                _set(self, attr, _freeze(current))

    def json_schema(self) -> dict[str, Any]:
        """The JSON schema of the arguments, as a fresh plain dict."""
        if self.input_schema is not None:
            return _thaw(self.input_schema)
        if self.input_model is not None:
            return self.input_model.model_json_schema()
        return _thaw(_EMPTY_SCHEMA)

    def signature(self) -> tuple[Any, ...]:
        """What the registry compares: name, description, schemas, tags and annotations."""
        return (
            self.name,
            self.description,
            _canonical(self.json_schema()),
            _canonical(self.output_schema),
            self.title,
            tuple(sorted(self.tags)),
            _canonical(self.annotations),
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToolSpec) and self.signature() == other.signature()

    def __hash__(self) -> int:
        return hash(self.signature())


@dataclass(frozen=True)
class ToolCall:
    """One invocation of a tool, as seen by plugin hooks."""

    plugin: str
    tool: str
    exposed_name: str
    arguments: Mapping[str, Any]
    spec: ToolSpec
    request_id: str

    def with_arguments(self, arguments: Mapping[str, Any]) -> ToolCall:
        return dataclasses.replace(self, arguments=arguments)


@dataclass(frozen=True)
class RawResponse:
    """An upstream response, or a wrapped handler return value."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    content_type: str
    value: Any = NO_VALUE

    def __post_init__(self) -> None:
        _set(self, "headers", MappingProxyType(dict(self.headers)))

    def _charset(self) -> str:
        for part in self.content_type.split(";")[1:]:
            key, _, val = part.partition("=")
            if key.strip().lower() == "charset" and val.strip():
                return val.strip().strip("\"'")
        return "utf-8"

    def text(self) -> str:
        """Body decoded with the content-type charset (default utf-8), replacing bad bytes."""
        try:
            return self.body.decode(self._charset(), errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The parsed body, or the wrapped value for a handler response."""
        if self.value is not NO_VALUE:
            return self.value
        return json.loads(self.text())

    @classmethod
    def from_value(cls, value: Any) -> RawResponse:
        """Wrap a handler return value: str is text, bytes is binary, anything else JSON."""
        if isinstance(value, str):
            return cls(200, {}, value.encode("utf-8"), "text/plain", value)
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            return cls(200, {}, data, "application/octet-stream", data)
        body = json.dumps(value).encode("utf-8")
        return cls(200, {}, body, "application/json", value)


@dataclass(frozen=True)
class ToolResult:
    """What a tool call returns to the client."""

    text: str
    structured: Any = None
    is_error: bool = False
