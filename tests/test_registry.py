from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import httpx
import pytest

from any_mcp.config import PluginEntry, ToolSettings
from any_mcp.context import NoConfig, PluginContext
from any_mcp.discovery import LoadedPlugin
from any_mcp.errors import ConfigError
from any_mcp.plugin import Plugin
from any_mcp.registry import ExposedTool, ToolRegistry
from any_mcp.types import ToolSpec


class Rec(Plugin):
    """A plugin whose tools come from a mutable list and whose hooks record into ``events``."""

    def __init__(
        self,
        events: list[str],
        specs: Sequence[ToolSpec] = (),
        *,
        fail_setup: bool = False,
        fail_load: bool = False,
        fail_teardown: bool = False,
        load_delay: float = 0.0,
    ) -> None:
        self.events = events
        self.specs = list(specs)
        self.fail_setup = fail_setup
        self.fail_load = fail_load
        self.fail_teardown = fail_teardown
        self.load_delay = load_delay
        self.loads = 0

    async def setup(self) -> None:
        self.events.append(f"setup:{self.ctx.name}")
        if self.fail_setup:
            raise RuntimeError("setup boom")

    async def teardown(self) -> None:
        self.events.append(f"teardown:{self.ctx.name}")
        if self.fail_teardown:
            raise RuntimeError("teardown boom")

    async def load_tools(self) -> Sequence[ToolSpec]:
        self.events.append(f"load:{self.ctx.name}")
        self.loads += 1
        if self.load_delay:
            await asyncio.sleep(self.load_delay)
        self.events.append(f"loaded:{self.ctx.name}")
        if self.fail_load:
            raise RuntimeError("load boom\nsecond line")
        return list(self.specs)


def _spec(name: str, description: str = "A tool.", **kw) -> ToolSpec:
    return ToolSpec(name=name, description=description, **kw)


def _loaded(
    instance: Plugin,
    name: str,
    *,
    namespace: str | None = None,
    allow: Sequence[str] = (),
    tags: Sequence[str] = (),
    reload_every: float | None = None,
) -> LoadedPlugin:
    entry = PluginEntry(
        name=name,
        plugin="tests:Rec",
        namespace=namespace,
        allow=list(allow),
        tags=list(tags),
        reload_every=reload_every,
    )
    ctx = PluginContext(
        name=name,
        namespace=namespace if namespace is not None else name,
        config=NoConfig(),
        http=httpx.AsyncClient(),
        log=logging.getLogger(f"any_mcp.plugin.{name}"),
        response_budget_chars=1000,
    )
    instance.ctx = ctx
    return LoadedPlugin(entry, instance, ctx, 30.0, reload_every)


def _names(registry: ToolRegistry) -> list[str]:
    return [t.name for t in registry.tools()]


async def test_start_and_stop_lifecycle() -> None:
    events: list[str] = []
    a = _loaded(Rec(events, [_spec("x")]), "a")
    b = _loaded(Rec(events, [_spec("y")], fail_teardown=True), "b")
    c = _loaded(Rec(events, [_spec("z")]), "c")
    registry = ToolRegistry([a, b, c], ToolSettings())
    await registry.start()
    assert events == [
        "setup:a",
        "load:a",
        "loaded:a",
        "setup:b",
        "load:b",
        "loaded:b",
        "setup:c",
        "load:c",
        "loaded:c",
    ]
    events.clear()
    await registry.stop()
    # reverse order, continuing past b's failing teardown
    assert events == ["teardown:c", "teardown:b", "teardown:a"]
    events.clear()
    await registry.stop()
    assert events == []

    # a failing setup: earlier plugins torn down (reverse) first, ConfigError names the plugin
    events = []
    a = _loaded(Rec(events, [_spec("x")]), "a")
    b = _loaded(Rec(events, [_spec("y")]), "b")
    bad = _loaded(Rec(events, fail_setup=True), "bad")
    never = _loaded(Rec(events), "never")
    registry = ToolRegistry([a, b, bad, never], ToolSettings())
    with pytest.raises(ConfigError, match="'bad'") as info:
        await registry.start()
    assert "setup" in str(info.value)
    assert events == [
        "setup:a",
        "load:a",
        "loaded:a",
        "setup:b",
        "load:b",
        "loaded:b",
        "setup:bad",
        "teardown:b",
        "teardown:a",
    ]
    assert registry.tools() == []

    # a failing first load_tools: the plugin itself was set up, so it is torn down too
    events = []
    a = _loaded(Rec(events, [_spec("x")]), "a")
    bad = _loaded(Rec(events, fail_load=True), "bad")
    registry = ToolRegistry([a, bad], ToolSettings())
    with pytest.raises(ConfigError, match="'bad'") as info:
        await registry.start()
    assert "load_tools" in str(info.value)
    assert events == [
        "setup:a",
        "load:a",
        "loaded:a",
        "setup:bad",
        "load:bad",
        "loaded:bad",
        "teardown:bad",
        "teardown:a",
    ]


async def test_names_filters_and_collisions() -> None:
    events: list[str] = []
    gh = _loaded(
        Rec(
            events,
            [
                _spec("list_issues", tags={"read"}),
                _spec("create_issue", tags={"write"}),
                _spec("list_prs", tags={"read", "pr"}),
                _spec("delete_repo", tags={"admin"}),
            ],
        ),
        "gh",
        allow=["list_*", "create_*"],
    )
    tagged = _loaded(
        Rec(events, [_spec("b", tags={"x"}), _spec("a", tags={"y"}), _spec("c")]),
        "tg",
        tags=["x", "y"],
    )
    bare = _loaded(Rec(events, [_spec("zeta"), _spec("alpha.v2 beta")]), "bare", namespace="")
    long = _loaded(Rec(events, [_spec("t" * 80)]), "long")
    settings = ToolSettings(max_name_length=30)
    registry = ToolRegistry([gh, tagged, bare, long], settings)
    await registry.start()
    names = _names(registry)
    assert names == sorted(names)
    long_name = next(n for n in names if n.startswith("long_"))
    assert len(long_name) == 30
    assert set(names) == {
        "gh_list_issues",
        "gh_create_issue",
        "gh_list_prs",
        "tg_b",
        "tg_a",
        "zeta",
        "alpha.v2_beta",
        long_name,
    }
    tool = registry.get("gh_list_issues")
    assert isinstance(tool, ExposedTool)
    assert tool.plugin is gh
    assert tool.spec.name == "list_issues"
    assert registry.get("gh_delete_repo") is None
    assert registry.get("tg_c") is None
    await registry.stop()

    # two tools with one exposed name: ConfigError naming both plugins and tools
    events = []
    one = _loaded(Rec(events, [_spec("b_c")]), "a")
    two = _loaded(Rec(events, [_spec("c")]), "a_b")
    registry = ToolRegistry([one, two], ToolSettings())
    with pytest.raises(ConfigError) as info:
        await registry.start()
    message = str(info.value)
    assert "a_b_c" in message
    assert "'a'" in message and "'a_b'" in message
    assert "'b_c'" in message and "'c'" in message
    assert events[-2:] == ["teardown:a_b", "teardown:a"]

    # a collision inside one plugin is caught as well
    events = []
    dup = _loaded(Rec(events, [_spec("x y"), _spec("x_y")]), "d")
    registry = ToolRegistry([dup], ToolSettings())
    with pytest.raises(ConfigError, match="d_x_y"):
        await registry.start()


async def test_schema_normalization_applied() -> None:
    events: list[str] = []
    schema = {
        "type": "object",
        "properties": {"pet": {"$ref": "#/$defs/Pet"}},
        "required": ["pet"],
        "$defs": {"Pet": {"type": "object", "properties": {"name": {"type": "string"}}}},
    }
    union = {
        "anyOf": [
            {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
            {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
        ]
    }
    out = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    plugin = _loaded(
        Rec(
            events,
            [
                _spec("ref", input_schema=schema, output_schema=out),
                _spec("union", input_schema=union),
                _spec("none"),
            ],
        ),
        "p",
    )
    registry = ToolRegistry([plugin], ToolSettings())
    await registry.start()

    ref = registry.get("p_ref")
    assert ref is not None
    assert ref.input_schema == {
        "type": "object",
        "properties": {"pet": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "required": ["pet"],
    }
    assert ref.listed_schema == ref.input_schema
    assert ref.output_schema == out and type(ref.output_schema) is dict

    union_tool = registry.get("p_union")
    assert union_tool is not None
    assert union_tool.input_schema == union  # validation keeps the original
    assert union_tool.listed_schema == {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "slug": {"type": "string"}},
    }

    none = registry.get("p_none")
    assert none is not None
    assert none.listed_schema == {"type": "object", "additionalProperties": False}
    assert none.output_schema is None
    await registry.stop()

    # a SchemaError names plugin and tool and becomes a ConfigError
    events = []
    for bad_schema in (
        {"type": "array", "items": {"type": "string"}},
        {"type": "object", "properties": {"x": {"$ref": "http://elsewhere/x.json"}}},
    ):
        bad = _loaded(Rec(events, [_spec("broken", input_schema=bad_schema)]), "q")
        registry = ToolRegistry([bad], ToolSettings())
        with pytest.raises(ConfigError) as info:
            await registry.start()
        assert type(info.value) is ConfigError
        assert "'q'" in str(info.value) and "'broken'" in str(info.value)


async def test_reload_diff_notifies_on_change() -> None:
    events: list[str] = []
    rec = Rec(events, [_spec("a")])
    other = Rec(events, [_spec("b")])
    registry = ToolRegistry([_loaded(rec, "p"), _loaded(other, "o")], ToolSettings())
    calls: list[int] = []

    async def listener() -> None:
        await asyncio.sleep(0)
        calls.append(1)

    registry.add_listener(listener)
    await registry.start()
    assert calls == []

    assert await registry.reload() is False
    assert await registry.reload("p") is False
    assert calls == []

    # a fresh but identical spec is not a change
    rec.specs = [_spec("a")]
    assert await registry.reload("p") is False
    assert calls == []

    changes = [
        [_spec("a", description="Changed.")],
        [_spec("a", description="Changed.", input_schema={"type": "object", "properties": {}})],
        [_spec("a", description="Changed.", tags={"t"})],
        [_spec("a", description="Changed.", tags={"t"}, annotations={"readOnlyHint": True})],
        [_spec("a2")],
        [],
    ]
    for specs in changes:
        rec.specs = specs
        before = len(calls)
        assert await registry.reload("p") is True
        assert len(calls) == before + 1
    assert _names(registry) == ["o_b"]

    second: list[int] = []

    async def other_listener() -> None:
        second.append(1)

    registry.add_listener(other_listener)
    rec.specs = [_spec("a")]
    assert await registry.reload() is True
    assert len(second) == 1
    assert _names(registry) == ["o_b", "p_a"]
    with pytest.raises(ConfigError, match="nope"):
        await registry.reload("nope")
    await registry.stop()


async def test_failed_reload_keeps_tools(caplog: pytest.LogCaptureFixture) -> None:
    events: list[str] = []
    rec = Rec(events, [_spec("a"), _spec("b")])
    other = Rec(events, [_spec("c")])
    registry = ToolRegistry([_loaded(rec, "p"), _loaded(other, "o")], ToolSettings())
    notified: list[int] = []

    async def listener() -> None:
        notified.append(1)

    registry.add_listener(listener)
    await registry.start()
    before = registry.tools()

    rec.fail_load = True
    other.specs = [_spec("c"), _spec("d")]
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="any_mcp"):
        changed = await registry.reload()
    assert changed is True  # o changed, p kept its tools
    assert _names(registry) == ["o_c", "o_d", "p_a", "p_b"]
    assert [t for t in registry.tools() if t.plugin.entry.name == "p"] == [
        t for t in before if t.plugin.entry.name == "p"
    ]
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    line = records[0].getMessage()
    assert "\n" not in line
    assert "'p'" in line and "load boom" in line
    assert len(notified) == 1

    # only the failing plugin: no change, no notification, still one line
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="any_mcp"):
        assert await registry.reload("p") is False
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
    assert len(notified) == 1

    # a reload that yields a bad schema or a collision also keeps the previous tools
    rec.fail_load = False
    rec.ctx.namespace = "p"
    for bad in (
        [_spec("a", input_schema={"type": "string"})],  # SchemaError
        [_spec("x y"), _spec("x_y")],  # collision inside the plugin
        "o",  # collision with o_c under a namespace switched to o
    ):
        if bad == "o":
            rec.ctx.namespace = "o"
            rec.specs = [_spec("c")]
        else:
            rec.specs = bad
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="any_mcp"):
            assert await registry.reload("p") is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1 and "'p'" in warnings[0].getMessage()
        assert _names(registry) == ["o_c", "o_d", "p_a", "p_b"]
    assert len(notified) == 1
    await registry.stop()


async def test_reloads_serialised() -> None:
    events: list[str] = []
    a = Rec(events, [_spec("a")], load_delay=0.02)
    b = Rec(events, [_spec("b")], load_delay=0.02)
    registry = ToolRegistry([_loaded(a, "a"), _loaded(b, "b")], ToolSettings())
    await registry.start()
    events.clear()

    seen: list[list[str]] = []

    async def watch() -> None:
        # a concurrent reader never sees a half-reloaded list
        for _ in range(20):
            seen.append(_names(registry))
            await asyncio.sleep(0.005)

    a.specs = [_spec("a"), _spec("a2")]
    b.specs = [_spec("b"), _spec("b2")]
    results = await asyncio.gather(
        registry.reload(), registry.reload("a"), registry.reload("b"), watch()
    )
    assert results[:3] == [True, False, False]
    # every load_tools call finishes before the next one starts
    assert len(events) == 8
    for i in range(0, len(events), 2):
        start, end = events[i], events[i + 1]
        assert start.startswith("load:") and end == "loaded:" + start.split(":", 1)[1]
    allowed = (["a_a", "b_b"], ["a_a", "a_a2", "b_b", "b_b2"])
    assert all(names in allowed for names in seen)
    await registry.stop()


async def test_periodic_reload() -> None:
    events: list[str] = []
    fast = Rec(events, [_spec("f")])
    still = Rec(events, [_spec("s")])
    registry = ToolRegistry(
        [_loaded(fast, "fast", reload_every=0.05), _loaded(still, "still")], ToolSettings()
    )
    await registry.start()
    assert fast.loads == 1 and still.loads == 1
    runner = asyncio.create_task(registry.run_periodic())
    await asyncio.sleep(0.03)
    assert fast.loads == 1  # the first periodic reload waits reload_every
    await asyncio.sleep(0.2)
    count = fast.loads
    assert 3 <= count <= 6
    assert still.loads == 1
    await registry.stop()
    await asyncio.wait_for(runner, 1)
    assert runner.exception() is None
    await asyncio.sleep(0.15)
    assert fast.loads == count

    # with no periodic plugin it returns at once
    idle = ToolRegistry([_loaded(Rec(events, [_spec("i")]), "idle")], ToolSettings())
    await idle.start()
    await asyncio.wait_for(idle.run_periodic(), 1)
    await idle.stop()


async def test_request_reload() -> None:
    events: list[str] = []
    rec = Rec(events, [_spec("a")])
    other = Rec(events, [_spec("b")])
    p = _loaded(rec, "p")
    o = _loaded(other, "o")
    registry = ToolRegistry([p, o], ToolSettings())
    changed = asyncio.Event()

    async def listener() -> None:
        changed.set()

    registry.add_listener(listener)
    await registry.start()
    rec.specs = [_spec("a"), _spec("new")]
    p.ctx.request_reload()
    await asyncio.wait_for(changed.wait(), 1)
    assert _names(registry) == ["o_b", "p_a", "p_new"]
    assert rec.loads == 2 and other.loads == 1

    # the registry method directly
    changed.clear()
    other.specs = []
    registry.request_reload("o")
    await asyncio.wait_for(changed.wait(), 1)
    assert _names(registry) == ["p_a", "p_new"]

    # from another thread, as a sync watcher callback would
    changed.clear()
    other.specs = [_spec("b")]
    await asyncio.to_thread(o.ctx.request_reload)
    await asyncio.wait_for(changed.wait(), 1)
    assert _names(registry) == ["o_b", "p_a", "p_new"]

    # after stop the request is ignored
    await registry.stop()
    rec.specs = []
    p.ctx.request_reload()
    await asyncio.sleep(0.02)
    assert rec.loads == 2
