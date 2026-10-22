import json
from importlib.metadata import version

import httpx
import pytest

from any_mcp.errors import ConfigError, ToolInputError, UpstreamError
from any_mcp.http import execute_binding, make_client
from any_mcp.types import HttpBinding, RawResponse, ToolCall, ToolSpec


def make_call(binding: HttpBinding, arguments: dict) -> ToolCall:
    spec = ToolSpec(name="t", description="d", http=binding)
    return ToolCall("p", "t", "p_t", arguments, spec, "r1")


def recording(status=200, body=b"{}", ctype="application/json"):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    return seen, make_client("https://api.test", transport=httpx.MockTransport(handler))


async def test_path_parameters():
    seen, client = recording()
    call = make_call(HttpBinding("GET", "/items/{id}/x/{name}"), {"id": 7, "name": "a b/c"})
    await execute_binding(client, call)
    assert seen[0].url.raw_path == b"/items/7/x/a%20b%2Fc"
    with pytest.raises(ToolInputError):
        await execute_binding(client, make_call(HttpBinding("GET", "/items/{id}"), {}))


async def test_query_parameters():
    seen, client = recording()
    binding = HttpBinding("GET", "/s", query=("a", "b", "c", "d", "e"))
    args = {"a": [1, 2], "b": None, "c": True, "d": False, "e": "x y"}
    await execute_binding(client, make_call(binding, args))
    assert seen[0].url.params.multi_items() == [
        ("a", "1"),
        ("a", "2"),
        ("c", "true"),
        ("d", "false"),
        ("e", "x y"),
    ]


async def test_body_modes():
    seen, client = recording()
    args = {"id": 1, "q": "z", "h": "hv", "a": 1, "b": [2]}
    base = {"query": ("q",), "headers": {"h": "X-H"}}

    async def body_for(body):
        binding = HttpBinding("POST", "/p/{id}", body=body, **base)
        await execute_binding(client, make_call(binding, args))
        raw = seen[-1].content
        return json.loads(raw) if raw else None

    assert await body_for(None) is None
    assert await body_for("*") == {"a": 1, "b": [2]}
    assert await body_for("b") == [2]
    assert await body_for(("a", "b")) == {"a": 1, "b": [2]}
    assert await body_for(("a", "missing")) == {"a": 1}


async def test_headers_and_credentials():
    seen, client = recording()
    binding = HttpBinding("GET", "/h", headers={"tok": "X-Token", "o": "X-Other"})
    call = make_call(binding, {"tok": "arg", "o": "v", "n": None})
    await execute_binding(client, call, headers={"X-Token": "cred", "Authorization": "Bearer z"})
    req = seen[0]
    assert req.headers["x-token"] == "cred"
    assert req.headers["x-other"] == "v"
    assert req.headers["authorization"] == "Bearer z"
    assert req.headers["user-agent"].startswith("any-mcp/")
    assert req.headers.get_list("x-token") == ["cred"]


async def test_credentials_win_case_insensitively():
    seen, client = recording()
    binding = HttpBinding("GET", "/h", headers={"tok": "X-Token"})
    await execute_binding(client, make_call(binding, {"tok": "arg"}), headers={"x-token": "cred"})
    assert seen[0].headers.get_list("x-token") == ["cred"]


async def test_base_url_resolution():
    seen, client = recording()
    binding = HttpBinding("GET", "/p", base_url="https://other.test/v1")
    await execute_binding(client, make_call(binding, {}))
    assert str(seen[0].url) == "https://other.test/v1/p"
    await execute_binding(client, make_call(HttpBinding("GET", "/p"), {}))
    assert str(seen[1].url) == "https://api.test/p"
    bare = make_client("", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ConfigError):
        await execute_binding(bare, make_call(HttpBinding("GET", "/p"), {}))
    ok = await execute_binding(bare, make_call(HttpBinding("GET", "http://abs.test/p"), {}))
    assert ok.status == 200


async def test_status_handling():
    _, client = recording(201, b"hi", "text/plain")
    res = await execute_binding(client, make_call(HttpBinding("GET", "/p"), {}))
    assert isinstance(res, RawResponse)
    assert (res.status, res.body, res.content_type) == (201, b"hi", "text/plain")
    assert res.headers["content-type"] == "text/plain"
    _, client = recording(404, b"nope", "text/plain")
    with pytest.raises(UpstreamError) as info:
        await execute_binding(client, make_call(HttpBinding("GET", "/p"), {}))
    assert info.value.response.status == 404
    assert info.value.response.body == b"nope"


async def test_make_client_defaults():
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    client = make_client("https://api.test", transport=transport)
    assert client.headers["user-agent"] == f"any-mcp/{version('any-mcp')}"
    assert "authorization" not in client.headers
    assert "cookie" not in client.headers
    assert client._transport is transport
    assert client.timeout.read == 30.0
    assert str(client.base_url).startswith("https://api.test")
