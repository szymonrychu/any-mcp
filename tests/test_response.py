from any_mcp.response import shape_default
from any_mcp.types import RawResponse


def raw(body: bytes, ctype: str, status: int = 200) -> RawResponse:
    return RawResponse(status, {}, body, ctype)


def test_json_body():
    r = shape_default(raw('{"a": "é", "b": [1, 2]}'.encode(), "application/json"))
    assert r.structured == {"a": "é", "b": [1, 2]}
    assert r.text == '{"a":"é","b":[1,2]}'
    assert not r.is_error


def test_json_suffix_content_type():
    r = shape_default(raw(b'{"a": 1}', "application/vnd.api+json; charset=utf-8"))
    assert r.structured == {"a": 1}
    assert r.text == '{"a":1}'


def test_text_body():
    r = shape_default(raw(b"hello", "text/plain"))
    assert r.text == "hello"
    assert r.structured is None


def test_binary_body():
    r = shape_default(raw(b"\x00\x01\x02", "image/png"))
    assert r.text == "3 bytes of image/png"
    assert r.structured is None


def test_empty_body():
    r = shape_default(raw(b"", "application/json", 204))
    assert r.text == "OK (204)"
    assert r.structured is None


def test_handler_values():
    s = shape_default(RawResponse.from_value("plain"))
    assert (s.text, s.structured) == ("plain", None)
    d = shape_default(RawResponse.from_value({"k": 1}))
    assert d.structured == {"k": 1} and d.text == '{"k":1}'
    li = shape_default(RawResponse.from_value([1, 2]))
    assert li.structured == [1, 2] and li.text == "[1,2]"


def test_over_budget_truncates():
    r = shape_default(raw(b"x" * 100, "text/plain"), budget_chars=10)
    assert r.text == "x" * 10 + "\n[truncated: 10 of 100 chars]"
    assert r.structured is None and not r.is_error
    j = shape_default(RawResponse.from_value({"k": "y" * 100}), budget_chars=10)
    assert j.structured is None
    assert j.text.startswith('{"k":"yyyy') and "[truncated: 10 of 108 chars]" in j.text


def test_over_budget_with_output_schema_is_error():
    r = shape_default(
        RawResponse.from_value({"k": "y" * 100}), budget_chars=10, output_schema={"type": "object"}
    )
    assert r.is_error
    assert "108" in r.text and "10" in r.text
