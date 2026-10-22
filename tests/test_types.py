import asyncio
import dataclasses

import pytest
from pydantic import BaseModel

from any_mcp.errors import (
    AnyMcpError,
    ConfigError,
    Deny,
    ToolInputError,
    UnknownTool,
    UpstreamError,
    error_result,
)
from any_mcp.types import NO_VALUE, HttpBinding, RawResponse, ToolCall, ToolResult, ToolSpec


class Args(BaseModel):
    x: int


def test_toolspec_validation():
    ToolSpec(name="a", description="d")
    with pytest.raises(ValueError):
        ToolSpec(name="", description="d")
    with pytest.raises(ValueError):
        ToolSpec(name="a", description="")
    with pytest.raises(ValueError):
        ToolSpec(name="a", description="d", input_schema={}, input_model=Args)

    async def h(call):
        return 1

    with pytest.raises(ValueError):
        ToolSpec(name="a", description="d", http=HttpBinding("GET", "/x"), handler=h)
    a = ToolSpec(name="a", description="d", input_schema={"type": "object"}, tags=frozenset({"t"}))
    b = ToolSpec(name="a", description="d", input_schema={"type": "object"}, tags=frozenset({"t"}))
    assert a == b and hash(a) == hash(b)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.name = "z"  # type: ignore[misc]


def test_toolspec_json_schema():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    assert ToolSpec(name="a", description="d", input_schema=schema).json_schema() == schema
    assert (
        ToolSpec(name="a", description="d", input_model=Args).json_schema()
        == Args.model_json_schema()
    )
    assert ToolSpec(name="a", description="d").json_schema() == {
        "type": "object",
        "additionalProperties": False,
    }


def test_raw_response_helpers():
    r = RawResponse(200, {}, b'{"a": [1]}', "application/json")
    assert r.json() == {"a": [1]}
    assert r.text() == '{"a": [1]}'
    latin = RawResponse(200, {}, "caf\xe9".encode("latin-1"), "text/plain; charset=latin-1")
    assert latin.text() == "caf\xe9"
    bad = RawResponse(200, {}, b"a\xffb", "text/plain")
    assert bad.text() == "a�b"
    s = RawResponse.from_value("hi")
    assert (s.status, s.content_type, s.body, s.json(), s.text()) == (
        200,
        "text/plain",
        b"hi",
        "hi",
        "hi",
    )
    b = RawResponse.from_value(b"\x00\x01")
    assert b.content_type == "application/octet-stream" and b.body == b"\x00\x01"
    assert b.json() == b"\x00\x01"
    for v in ({"a": 1}, [1, 2], 3, 1.5, True, None):
        rv = RawResponse.from_value(v)
        assert rv.content_type == "application/json" and rv.json() == v and rv.status == 200
    assert RawResponse.from_value(None).value is None
    assert RawResponse(200, {}, b"", "x").value is NO_VALUE


def test_error_result_kinds():
    d = error_result(Deny("no way"), plugin="p")
    assert d.is_error and d.structured == {"error": {"kind": "denied", "message": "no way"}}
    assert "no way" in d.text and "\n" not in d.text

    e = error_result(ToolInputError([("$.a", "bad"), ("$.b", "worse")]), plugin="p")
    assert e.structured["error"]["kind"] == "invalid_arguments"
    for s in ("$.a", "bad", "$.b", "worse"):
        assert s in e.text and s in e.structured["error"]["message"]

    u = error_result(UpstreamError(RawResponse(404, {}, b"missing", "text/plain")), plugin="p")
    assert u.structured["error"]["kind"] == "upstream" and u.structured["error"]["status"] == 404
    assert "404" in u.text and "missing" in u.text

    for t in (TimeoutError(), asyncio.TimeoutError()):  # noqa: UP041
        assert error_result(t, plugin="p").structured["error"]["kind"] == "timeout"

    i = error_result(ValueError("secret"), plugin="p")
    assert i.structured == {"error": {"kind": "internal", "message": "p failed: ValueError"}}
    assert "secret" not in i.text
    i2 = error_result(ValueError("secret"), plugin="p", expose_details=True)
    assert "secret" in i2.text and "p failed: ValueError" in i2.text
    for c in (Deny, ToolInputError, UnknownTool, ConfigError):
        assert issubclass(c, AnyMcpError)
    assert ToolResult("x").is_error is False


def test_error_text_has_no_headers():
    resp = RawResponse(500, {"Authorization": "Bearer TOPSECRET"}, b"x" * 2000, "text/plain")
    u = error_result(UpstreamError(resp), plugin="p")
    assert u.text.count("x") == 500
    assert "TOPSECRET" not in u.text and "TOPSECRET" not in str(u.structured)
    call = ToolCall("p", "t", "p_t", {"k": 1}, ToolSpec(name="t", description="d"), "r1")
    assert call.with_arguments({"k": 2}).arguments == {"k": 2}
    assert call.arguments == {"k": 1}
