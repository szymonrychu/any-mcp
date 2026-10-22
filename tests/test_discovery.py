from __future__ import annotations

import sys
import textwrap
from importlib.metadata import EntryPoint
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from any_mcp import discovery
from any_mcp.config import AnyMcpConfig
from any_mcp.discovery import load_plugins, resolve_plugin_class
from any_mcp.errors import ConfigError
from any_mcp.plugin import Plugin, tool


class Cfg(BaseModel):
    base_url: str = "https://api.example.com"
    limit: int = 5


class Demo(Plugin):
    name = "demo"
    Config = Cfg
    timeout = 12.0
    reload_every = 60.0

    @tool
    async def ping(self) -> str:
        """Ping."""
        return "pong"


class NotAPlugin:
    pass


def _config(*plugins: dict, **extra) -> AnyMcpConfig:
    return AnyMcpConfig.model_validate({"plugins": list(plugins), **extra})


def test_module_class_reference(tmp_path: Path) -> None:
    (tmp_path / "myplug_mod.py").write_text(
        textwrap.dedent(
            """
            from any_mcp.plugin import Plugin, tool

            class P(Plugin):
                @tool
                async def hi(self) -> str:
                    '''Hi.'''
                    return "hi"
            """
        )
    )
    before = sys.path.count(str(tmp_path))
    cls = resolve_plugin_class("myplug_mod:P", python_path=[str(tmp_path)])
    resolve_plugin_class("myplug_mod:P", python_path=[str(tmp_path)])
    assert cls.__name__ == "P" and issubclass(cls, Plugin)
    assert sys.path.count(str(tmp_path)) == before + 1
    sys.path.remove(str(tmp_path))
    sys.modules.pop("myplug_mod", None)


def test_entry_point_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = EntryPoint(name="demo-ep", value=f"{__name__}:Demo", group="any_mcp.plugins")
    seen: dict = {}

    def fake(**kw):
        seen.update(kw)
        return [ep] if kw.get("name") == "demo-ep" else []

    monkeypatch.setattr(discovery, "entry_points", fake)
    assert resolve_plugin_class("demo-ep") is Demo
    assert seen["group"] == "any_mcp.plugins"


def test_bad_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "entry_points", lambda **kw: [])
    for ref in ("no_such_mod_xyz:P", f"{__name__}:Missing", f"{__name__}:NotAPlugin", "nope-ep"):
        with pytest.raises(ConfigError, match=ref.split(":")[0]):
            resolve_plugin_class(ref)
    ep = EntryPoint(name="bad", value=f"{__name__}:NotAPlugin", group="any_mcp.plugins")
    monkeypatch.setattr(discovery, "entry_points", lambda **kw: [ep])
    with pytest.raises(ConfigError, match="bad"):
        resolve_plugin_class("bad")


def test_plugin_config_validation() -> None:
    cfg = _config({"name": "a", "plugin": f"{__name__}:Demo", "config": {"limit": "x"}})
    with pytest.raises(ConfigError, match=r"plugins\[0\]\.config\.limit"):
        load_plugins(cfg)
    cfg = _config(
        {"name": "a", "plugin": f"{__name__}:Demo"},
        {"name": "b", "plugin": f"{__name__}:Demo", "config": {"limit": "y"}},
    )
    with pytest.raises(ConfigError, match=r"plugins\[1\]\.config\.limit"):
        load_plugins(cfg)


def test_entry_overrides() -> None:
    cfg = _config(
        {"name": "a", "plugin": f"{__name__}:Demo"},
        {
            "name": "b",
            "plugin": f"{__name__}:Demo",
            "namespace": "ns",
            "timeout": 3,
            "reload_every": 9,
        },
    )
    a, b = load_plugins(cfg)
    assert (a.ctx.namespace, a.timeout, a.reload_every) == ("a", 12.0, 60.0)
    assert (b.ctx.namespace, b.timeout, b.reload_every) == ("ns", 3, 9)
    assert a.ctx.name == "a" and a.ctx.log.name == "any_mcp.plugin.a"
    assert a.ctx.response_budget_chars == 40000
    assert a.instance.ctx is a.ctx


async def test_context_http_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": str(request.url)})

    cfg = _config(
        {"name": "a", "plugin": f"{__name__}:Demo", "config": {"base_url": "https://x.test/v1/"}}
    )
    (lp,) = load_plugins(cfg, http_transport=httpx.MockTransport(handler))
    r = await lp.ctx.http.get("items")
    assert r.json()["url"] == "https://x.test/v1/items"
    assert lp.ctx.http.timeout.read == 12.0


def test_two_instances_of_one_class() -> None:
    cfg = _config(
        {"name": "a", "plugin": f"{__name__}:Demo"}, {"name": "b", "plugin": f"{__name__}:Demo"}
    )
    a, b = load_plugins(cfg)
    assert a.instance is not b.instance and a.ctx is not b.ctx and a.ctx.http is not b.ctx.http


def test_import_time_failure_is_config_error(tmp_path: Path) -> None:
    (tmp_path / "boom_plugin_mod.py").write_text("raise RuntimeError('kaboom')\n")
    (tmp_path / "syntax_plugin_mod.py").write_text("def (:\n")
    for mod, text in (("boom_plugin_mod", "kaboom"), ("syntax_plugin_mod", "syntax")):
        ref = f"{mod}:P"
        with pytest.raises(ConfigError, match=ref) as info:
            resolve_plugin_class(ref, python_path=[str(tmp_path)])
        assert text in str(info.value).lower()


async def test_loaded_plugins_aclose_closes_http_clients() -> None:
    cfg = _config(
        {"name": "a", "plugin": f"{__name__}:Demo"},
        {"name": "b", "plugin": f"{__name__}:Demo"},
    )
    loaded = load_plugins(cfg)
    assert [lp.ctx.http.is_closed for lp in loaded] == [False, False]
    await loaded.aclose()
    assert [lp.ctx.http.is_closed for lp in loaded] == [True, True]
    await loaded.aclose()  # a second call is a no-op


async def test_owners_close_http_clients_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    from any_mcp import cli
    from any_mcp._fastmcp_adapter import build_server, run_stdio
    from any_mcp.registry import ToolRegistry
    from any_mcp.testing import PluginHarness

    cfg = _config({"name": "a", "plugin": f"{__name__}:Demo"})
    made: list = []
    real = cli.load_plugins

    def spy(config: AnyMcpConfig):
        made.append(real(config))
        return made[-1]

    monkeypatch.setattr(cli, "load_plugins", spy)

    # CLI: --list-tools path
    await cli._list_tools(cfg)
    assert made[-1][0].ctx.http.is_closed

    # CLI: stdio path (stdin closes immediately -> server returns)
    async def fake_run_stdio(self, **_kw) -> None:
        return None

    monkeypatch.setattr("fastmcp.FastMCP.run_stdio_async", fake_run_stdio)
    await cli._serve_stdio(cfg)
    assert made[-1][0].ctx.http.is_closed

    # adapter: final shutdown of run_stdio closes; a server never run stays open
    loaded = load_plugins(cfg)
    server = build_server(ToolRegistry(loaded, cfg.tools), cfg, aclose=loaded.aclose)
    assert not loaded[0].ctx.http.is_closed
    await run_stdio(server)
    assert loaded[0].ctx.http.is_closed

    # harness
    async with PluginHarness(Demo) as h:
        clients = [lp.ctx.http for lp in h._loaded]
        assert not any(c.is_closed for c in clients)
    assert all(c.is_closed for c in clients)
