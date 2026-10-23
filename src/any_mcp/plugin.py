"""The plugin contract: one class, one required hook, every other hook defaulted."""

from __future__ import annotations

import dataclasses
import inspect
import typing
from abc import ABC
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, overload

from pydantic import BaseModel, TypeAdapter, create_model
from pydantic_core import to_jsonable_python
from typing_extensions import is_typeddict

from any_mcp.context import NoConfig, PluginContext
from any_mcp.errors import ConfigError, error_result
from any_mcp.http import execute_binding
from any_mcp.response import shape_default
from any_mcp.types import InputRequired, RawResponse, ToolCall, ToolResult, ToolSpec

_MARK = "__any_mcp_tool__"


@dataclasses.dataclass(frozen=True)
class _ToolOptions:
    name: str | None
    description: str | None
    tags: tuple[str, ...]
    annotations: Mapping[str, Any] | None


def _first_paragraph(doc: str) -> str:
    return " ".join(doc.strip().split("\n\n", 1)[0].split())


def _mark(fn: Callable[..., Any], opts: _ToolOptions) -> Callable[..., Any]:
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"@tool method {getattr(fn, '__name__', fn)!r} must be async")
    doc = inspect.getdoc(fn)
    if not opts.description and not (doc and doc.strip()):
        raise TypeError(
            f"@tool method {fn.__name__!r} needs a docstring (it becomes the description)"
        )
    setattr(fn, _MARK, opts)
    return fn


@overload
def tool(fn: Callable[..., Awaitable[Any]], /) -> Callable[..., Awaitable[Any]]: ...
@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    tags: Sequence[str] = (),
    annotations: Mapping[str, Any] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]: ...
def tool(
    fn: Callable[..., Awaitable[Any]] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    tags: Sequence[str] = (),
    annotations: Mapping[str, Any] | None = None,
) -> Any:
    """Mark an async Plugin method as a tool.

    The name comes from the function, the description from the first paragraph of the docstring,
    the input schema from the signature and the output schema from a model, TypedDict or
    dataclass return annotation.
    """
    opts = _ToolOptions(name, description, tuple(tags), annotations)
    if fn is not None:
        return _mark(fn, opts)
    return lambda f: _mark(f, opts)


def _input_model(fn: Callable[..., Any], hints: Mapping[str, Any], title: str) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for pname, param in list(inspect.signature(fn).parameters.items())[1:]:
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = hints.get(pname, Any)
        default = ... if param.default is param.empty else param.default
        fields[pname] = (annotation, default)
    return create_model(title, **fields)


def _output_schema(returns: Any) -> dict[str, Any] | None:
    if not isinstance(returns, type):
        return None
    if issubclass(returns, BaseModel):
        return returns.model_json_schema()
    if is_typeddict(returns) or dataclasses.is_dataclass(returns):
        return TypeAdapter(returns).json_schema()
    return None


def _build_spec(instance: Plugin, fn: Callable[..., Any], opts: _ToolOptions) -> ToolSpec:
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception as exc:
        raise TypeError(f"@tool method {fn.__name__!r}: cannot resolve annotations: {exc}") from exc
    name = opts.name or fn.__name__
    description = opts.description or _first_paragraph(inspect.getdoc(fn) or "")
    bound = getattr(instance, fn.__name__)

    async def handler(call: ToolCall) -> Any:
        value = await bound(**call.arguments)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return to_jsonable_python(value)

    return ToolSpec(
        name=name,
        description=description,
        input_model=_input_model(fn, hints, f"{name}_input"),
        output_schema=_output_schema(hints.get("return")),
        tags=frozenset(opts.tags),
        annotations=opts.annotations,
        handler=handler,
    )


def _tool_methods(cls: type) -> dict[str, Callable[..., Any]]:
    """Decorated methods, definition order, inherited first; an undecorated override drops one."""
    found: dict[str, Callable[..., Any]] = {}
    for klass in reversed(cls.__mro__):
        for attr, value in vars(klass).items():
            if hasattr(value, _MARK):
                found[attr] = value
            else:
                found.pop(attr, None)
    return found


def _is_abstract(cls: type) -> bool:
    if "_abstract" in vars(cls):
        return bool(vars(cls)["_abstract"])
    names = {
        a
        for k in cls.__mro__
        for a, v in vars(k).items()
        if getattr(v, "__isabstractmethod__", False)
    }
    return any(getattr(getattr(cls, n, None), "__isabstractmethod__", False) for n in names)


class Plugin(ABC):
    """Base class of every plugin: add ``@tool`` methods or override ``load_tools``.

    Class attributes: ``name`` (default namespace), ``Config`` (pydantic model for the plugin's
    config), ``reload_every`` (seconds between periodic reloads, None for never; this is only a
    default that a plugin entry's ``reload_every`` in the config file overrides) and ``timeout``
    (seconds per call). ``ctx`` is set by the core before ``setup()``.
    """

    name: ClassVar[str] = ""
    Config: ClassVar[type[BaseModel]] = NoConfig
    reload_every: ClassVar[float | None] = None
    timeout: ClassVar[float] = 30.0
    _abstract: ClassVar[bool] = False

    ctx: PluginContext

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if _is_abstract(cls):
            return
        if cls.load_tools is Plugin.load_tools and not _tool_methods(cls):
            raise TypeError(
                f"{cls.__name__} must override load_tools or define at least one @tool method"
            )

    @property
    def config(self) -> BaseModel:
        return self.ctx.config

    async def setup(self) -> None:
        """Open clients or caches. Runs once after ``ctx`` is set."""
        return None

    async def teardown(self) -> None:
        """Release what ``setup`` opened."""
        return None

    async def load_tools(self) -> Sequence[ToolSpec]:
        """The tools to offer; called at start and on every reload."""
        return [
            _build_spec(self, fn, fn.__any_mcp_tool__) for fn in _tool_methods(type(self)).values()
        ]

    async def credentials(self, call: ToolCall) -> Mapping[str, str]:
        """Outbound auth headers for an HTTP-bound call."""
        return {}

    async def before_call(self, call: ToolCall) -> ToolCall:
        """Rewrite the call or raise ``Deny``."""
        return call

    async def request_input(self, call: ToolCall) -> InputRequired | None:
        """Ask the client for more input, or return ``None`` to let the call run.

        Read the client's answers from ``call.input_responses`` and your own opaque state from
        ``call.request_state``. any-mcp passes the state through verbatim, so a plugin that needs
        tamper resistance signs or encrypts the value it puts in ``InputRequired.state``.
        """
        return None

    async def call(self, call: ToolCall) -> RawResponse:
        """Run the handler, or send the HTTP binding with ``credentials()`` headers."""
        spec = call.spec
        if spec.handler is not None:
            return RawResponse.from_value(await spec.handler(call))
        if spec.http is not None:
            return await execute_binding(
                self.ctx.http,
                call,
                headers=await self.credentials(call),
                # one byte past 4 per budgeted char: a cut body then always decodes to more
                # chars than the budget, so shape_response marks it as truncated
                max_bytes=self.ctx.response_budget_chars * 4 + 1,
            )
        raise ConfigError(f"tool {spec.name!r} has neither a handler nor an http binding")

    async def shape_response(self, call: ToolCall, raw: RawResponse) -> ToolResult:
        return shape_default(
            raw,
            budget_chars=self.ctx.response_budget_chars,
            output_schema=call.spec.output_schema,
        )

    async def on_error(self, call: ToolCall, exc: Exception) -> ToolResult:
        return error_result(exc, plugin=self.ctx.name)
