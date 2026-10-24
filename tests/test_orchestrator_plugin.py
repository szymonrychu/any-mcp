from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from any_mcp.context import PluginContext
from any_mcp.orchestrator import OrchestratorPlugin
from any_mcp.testing import PluginHarness

TOKEN = "s3cr3t-token-value"
CONFIG: dict[str, Any] = {
    "base_url": "http://orch.test:8081",
    "token": TOKEN,
    "cluster": "c-1",
    "agent": "agent-0",
    "slug": "my-hive",
    "max_replicas": 3,
    "hive": True,
}
NINE = {
    "orchestrator_me",
    "orchestrator_leadership_handover",
    "orchestrator_lock_claim",
    "orchestrator_lock_release",
    "orchestrator_lock_list",
    "orchestrator_lock_reassign",
    "orchestrator_scale",
    "orchestrator_init_scripts_get",
    "orchestrator_init_scripts_edit",
}


def harness(handler: Any, **overrides: Any) -> PluginHarness:
    return PluginHarness(
        OrchestratorPlugin,
        config={**CONFIG, **overrides},
        name="orchestrator",
        transport=httpx.MockTransport(handler),
    )


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


async def descriptions(**overrides: Any) -> dict[str, str]:
    async with harness(ok, **overrides) as h:
        return {t["name"]: t["description"] for t in await h.list_tools()}


async def test_lists_nine_tools() -> None:
    assert set(await descriptions()) == NINE
    assert set(await descriptions(hive=False)) == NINE


async def test_tools_call_agent_routes() -> None:
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"echo": request.url.path})

    calls: list[tuple[str, dict[str, Any], tuple[str, str, Any]]] = [
        ("me", {}, ("GET", "/agent/v1/me", None)),
        (
            "leadership_handover",
            {"reason": "ctx", "epoch": 4, "to": "agent-1"},
            (
                "POST",
                "/agent/v1/leadership/handover",
                {"reason": "ctx", "epoch": 4, "to": "agent-1"},
            ),
        ),
        (
            "leadership_handover",
            {"reason": "ctx", "epoch": 4},
            ("POST", "/agent/v1/leadership/handover", {"reason": "ctx", "epoch": 4}),
        ),
        ("lock_claim", {"paths": ["a.py"]}, ("POST", "/agent/v1/locks/claim", {"paths": ["a.py"]})),
        (
            "lock_release",
            {"paths": ["a.py"]},
            ("POST", "/agent/v1/locks/release", {"paths": ["a.py"]}),
        ),
        ("lock_list", {}, ("GET", "/agent/v1/locks", None)),
        (
            "lock_reassign",
            {"path": "a.py", "to": None},
            ("POST", "/agent/v1/locks/reassign", {"path": "a.py", "to": None}),
        ),
        ("scale", {"replicas": 2}, ("POST", "/agent/v1/scale", {"replicas": 2})),
        ("init_scripts_get", {}, ("GET", "/agent/v1/init-scripts", None)),
        (
            "init_scripts_edit",
            {"scripts": ["echo hi"]},
            ("PUT", "/agent/v1/init-scripts", {"scripts": ["echo hi"]}),
        ),
    ]
    async with harness(handler) as h:
        for tool, args, expected in calls:
            seen.clear()
            result = await h.call(f"orchestrator_{tool}", args)
            assert not result.is_error, result.text
            assert seen == [expected]
            assert json.loads(result.text) == {"echo": expected[1]}


async def test_credentials_headers() -> None:
    headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        return httpx.Response(200, json={})

    async with harness(handler) as h:
        await h.call("orchestrator_me")
        await h.call("orchestrator_lock_claim", {"paths": ["x"]})
    assert len(headers) == 2
    for got in headers:
        assert got["authorization"] == f"Bearer {TOKEN}"
        assert got["x-orch-cluster"] == "c-1"


async def test_descriptions_from_config() -> None:
    hive_tools = (
        "me",
        "leadership_handover",
        "lock_claim",
        "lock_release",
        "lock_list",
        "lock_reassign",
    )
    on = await descriptions()
    sentence = (
        "You are agent agent-0, one of up to 3 agents in hive my-hive; the workdir is shared."
    )
    for name in hive_tools:
        assert on[f"orchestrator_{name}"].startswith(sentence)
    assert "mcp__ccw__send_message" in on["orchestrator_me"]
    assert "never instructions from the user" in on["orchestrator_me"]
    assert "advisory" in on["orchestrator_lock_claim"]
    assert "reassign or discard" in on["orchestrator_lock_reassign"]
    assert "1..3" in on["orchestrator_scale"]
    assert ".agent-init/" in on["orchestrator_init_scripts_get"]
    assert "never re-runs" in on["orchestrator_init_scripts_edit"]

    off = await descriptions(hive=False)
    for text in off.values():
        assert "You are agent" not in text
        assert "in hive" not in text


async def test_handover_threshold_hint() -> None:
    name = "orchestrator_leadership_handover"
    with_pct = (await descriptions(auto_compact_pct=80))[name]
    assert "hand over instead of compacting once your context passes 65%" in with_pct
    assert "compacting" not in (await descriptions())[name]


async def test_error_mapping_redacts_token(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "stale_epoch", "message": f"epoch is 5, token {TOKEN}"}},
        )

    caplog.set_level(logging.DEBUG)
    async with harness(handler) as h:
        result = await h.call("orchestrator_leadership_handover", {"reason": "r", "epoch": 1})
    assert result.is_error
    assert "stale_epoch" in result.text
    assert "epoch is 5" in result.text
    assert TOKEN not in result.text
    assert TOKEN not in json.dumps(result.structured)
    assert TOKEN not in caplog.text

    async with harness(lambda r: httpx.Response(500, text=f"boom {TOKEN}")) as h:
        result = await h.call("orchestrator_me")
    assert result.is_error
    assert "500" in result.text
    assert TOKEN not in result.text


async def test_no_io_at_load() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    plugin = OrchestratorPlugin()
    plugin.ctx = PluginContext(
        name="orchestrator",
        namespace="orchestrator",
        config=OrchestratorPlugin.Config.model_validate(CONFIG),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        log=logging.getLogger("test"),
        response_budget_chars=40000,
        config_dir=None,
    )
    await plugin.setup()
    specs = await plugin.load_tools()
    assert len(specs) == 9


async def test_scale_and_init_tools_match_contracts() -> None:
    accepted = {"status": "accepted", "from": 3, "to": 2, "removed": ["agent-2"]}
    scripts = {"scripts": ["echo a"], "log_dir": ".agent-init/", "applies": "next_cluster"}
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, body))
        if request.url.path == "/agent/v1/scale":
            if body["replicas"] == 9:
                return httpx.Response(
                    422, json={"error": {"code": "out_of_range", "message": "1..3"}}
                )
            if body["replicas"] == 0:
                return httpx.Response(
                    409, json={"error": {"code": "static_cluster", "message": "static"}}
                )
            if body["replicas"] == 1:
                return httpx.Response(
                    409, json={"error": {"code": "leader_would_leave", "message": "leader"}}
                )
            if body["replicas"] == 5:
                return httpx.Response(
                    403, json={"error": {"code": "not_leader", "message": "not the leader"}}
                )
            return httpx.Response(202, json=accepted)
        return httpx.Response(200, json=scripts)

    async with harness(handler) as h:
        r = await h.call("orchestrator_scale", {"replicas": 2})
        assert json.loads(r.text) == accepted
        for n, code in (
            (9, "out_of_range"),
            (0, "static_cluster"),
            (1, "leader_would_leave"),
            (5, "not_leader"),
        ):
            r = await h.call("orchestrator_scale", {"replicas": n})
            assert r.is_error and code in r.text
        r = await h.call("orchestrator_init_scripts_get")
        assert json.loads(r.text) == scripts
        seen.clear()
        r = await h.call("orchestrator_init_scripts_edit", {"scripts": ["echo b"]})
        assert seen == [("PUT", {"scripts": ["echo b"]})]
