import logging
from dataclasses import dataclass
from typing import Annotated, TypedDict

import httpx
import pydantic
import pytest
from pydantic import BaseModel, Field

import any_mcp
from any_mcp import (
    NoConfig,
    Plugin,
    PluginContext,
    RawResponse,
    ToolCall,
    ToolSpec,
    tool,
)
from any_mcp.config import PluginEntry
from any_mcp.errors import ConfigError, Deny, UpstreamError
from any_mcp.http import make_client
from any_mcp.types import HttpBinding


def make_ctx(config=None, http=None, name="p", budget=1000) -> PluginContext:
    return PluginContext(
        name=name,
        namespace=name,
        config=config if config is not None else NoConfig(),
        http=http or make_client("https://api.test"),
        log=logging.getLogger("test"),
        response_budget_chars=budget,
    )


def make_call(plugin: Plugin, spec: ToolSpec, arguments=None) -> ToolCall:
    return ToolCall("p", spec.name, "p_" + spec.name, arguments or {}, spec, "r1")


def test_subclass_must_provide_tools():
    with pytest.raises(TypeError, match="load_tools"):

        class Empty(Plugin):
            pass

    class WithTool(Plugin):
        @tool
        async def a(self) -> str:
            """Do a."""
            return "a"

    class WithLoad(Plugin):
        async def load_tools(self):
            return []

    class Base(Plugin):
        _abstract = True

    class Child(Base):
        @tool
        async def b(self) -> str:
            """Do b."""
            return "b"

    with pytest.raises(TypeError, match="load_tools"):

        class Child2(Base):
            pass


def test_tool_decorator_builds_spec():
    class Item(BaseModel):
        id: int

    class Out(TypedDict):
        n: int

    @dataclass
    class DC:
        x: int

    class P(Plugin):
        @tool(tags=("a", "b"), annotations={"readOnlyHint": True})
        async def find(
            self,
            q: Annotated[str, Field(description="query")],
            limit: int = 5,
            opt: str | None = None,
        ) -> Item:
            """Find things.

            Second paragraph ignored.
            """
            return Item(id=1)

        @tool(name="renamed", description="Custom")
        async def other(self) -> Out:
            """Doc."""
            return {"n": 1}

        @tool
        async def dc(self) -> DC:
            """Dc."""
            return DC(1)

    p = P()
    p.ctx = make_ctx()
    specs = {s.name: s for s in _run(p.load_tools())}
    find = specs["find"]
    assert find.description == "Find things."
    assert find.tags == frozenset({"a", "b"})
    assert find.annotations == {"readOnlyHint": True}
    schema = find.json_schema()
    assert schema["required"] == ["q"]
    assert schema["properties"]["q"]["description"] == "query"
    assert schema["properties"]["limit"]["default"] == 5
    assert "self" not in schema["properties"]
    assert find.input_model(q="x", limit=2).limit == 2
    assert find.output_schema["properties"]["id"]["type"] == "integer"
    assert specs["renamed"].description == "Custom"
    assert specs["renamed"].output_schema["properties"]["n"]["type"] == "integer"
    assert specs["dc"].output_schema["properties"]["x"]["type"] == "integer"


def test_tool_requires_async_and_docstring():
    with pytest.raises(TypeError, match="async"):

        @tool
        def sync(self):
            """Doc."""

    with pytest.raises(TypeError, match="docstring"):

        @tool
        async def nodoc(self):
            pass

    with pytest.raises(TypeError, match="async"):
        tool(name="x")(lambda self: 1)


def test_default_load_tools():
    class Base(Plugin):
        @tool
        async def one(self) -> str:
            """One."""
            return "1"

        @tool
        async def two(self) -> str:
            """Two."""
            return "2"

    class Child(Base):
        @tool
        async def three(self) -> str:
            """Three."""
            return "3"

    c = Child()
    c.ctx = make_ctx()
    assert [s.name for s in _run(c.load_tools())] == ["one", "two", "three"]


async def test_default_call_paths():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    class P(Plugin):
        async def credentials(self, call):
            return {"Authorization": "Bearer t"}

        @tool
        async def hi(self, who: str) -> str:
            """Say hi."""
            return f"hi {who}"

    p = P()
    p.ctx = make_ctx(http=make_client("https://api.test", transport=httpx.MockTransport(handler)))

    spec = (await _tools(p))[0]
    raw = await p.call(make_call(p, spec, {"who": "bob"}))
    assert raw.json() == "hi bob"

    http_spec = ToolSpec(name="g", description="d", http=HttpBinding("GET", "/x"))
    raw = await p.call(make_call(p, http_spec))
    assert raw.json() == {"ok": True}
    assert seen[0].headers["authorization"] == "Bearer t"

    with pytest.raises(ConfigError):
        await p.call(make_call(p, ToolSpec(name="n", description="d")))


async def test_default_shape_and_error():
    class P(Plugin):
        async def load_tools(self):
            return []

    p = P()
    p.ctx = make_ctx(name="myplug", budget=10)
    spec = ToolSpec(name="t", description="d")
    call = make_call(p, spec)
    res = await p.shape_response(call, RawResponse.from_value("x" * 50))
    assert "truncated" in res.text and not res.is_error
    err = await p.on_error(call, Deny("nope"))
    assert err.is_error and "nope" in err.text
    err = await p.on_error(call, RuntimeError("secret"))
    assert "myplug" in err.text and "secret" not in err.text
    assert await p.credentials(call) == {}
    assert await p.before_call(call) is call
    await p.setup()
    await p.teardown()


def test_context():
    class Cfg(BaseModel):
        base_url: str = "x"

    class P(Plugin):
        Config = Cfg

        async def load_tools(self):
            return []

    ctx = make_ctx(config=Cfg(base_url="y"))
    p = P()
    p.ctx = ctx
    assert p.config.base_url == "y"
    assert P.Config is Cfg and Plugin.Config is NoConfig
    ctx.request_reload()  # unbound: no-op
    calls = []
    ctx._bind_reload(lambda: calls.append(1))
    ctx.request_reload()
    assert calls == [1]
    assert (P.name, P.reload_every, P.timeout) == ("", None, 30.0)


async def test_hello_world_minimal():
    class Hello(Plugin):
        name = "hello"

        @tool
        async def greet(self, who: str = "world") -> str:
            """Greet someone."""
            return f"hello {who}"

    p = Hello()
    p.ctx = make_ctx(name="hello")
    spec = (await _tools(p))[0]
    call = make_call(p, spec, {"who": "you"})
    raw = await p.call(call)
    res = await p.shape_response(call, raw)
    assert res.text == "hello you" and not res.is_error


def test_public_exports():
    for n in [
        "Plugin",
        "tool",
        "ToolSpec",
        "HttpBinding",
        "ToolCall",
        "RawResponse",
        "ToolResult",
        "Deny",
        "UpstreamError",
        "ToolInputError",
        "PluginContext",
        "NoConfig",
    ]:
        assert hasattr(any_mcp, n), n
    assert any_mcp.__version__
    assert issubclass(any_mcp.NoConfig, pydantic.BaseModel)
    assert any_mcp.UpstreamError is UpstreamError


def test_reload_every_class_default_overridable_by_config():
    class Slow(Plugin):
        reload_every = 60.0

        async def load_tools(self):
            return []

    assert Slow.reload_every == 60.0
    assert "overrid" in (Plugin.__doc__ or "").lower()
    entry = PluginEntry(name="s", plugin="m:Slow", reload_every=5)
    effective = entry.reload_every if entry.reload_every is not None else Slow.reload_every
    assert effective == 5
    entry = PluginEntry(name="s", plugin="m:Slow")
    effective = entry.reload_every if entry.reload_every is not None else Slow.reload_every
    assert effective == 60.0


async def _tools(p):
    return list(await p.load_tools())


def _run(coro):
    import asyncio

    return asyncio.run(coro)
