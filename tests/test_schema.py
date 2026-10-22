"""Schema normalisation, tool naming and argument validation."""

from __future__ import annotations

import hashlib

import pytest

from any_mcp.errors import ConfigError, ToolInputError
from any_mcp.schema import SchemaError, exposed_name, listing_schema, normalize_schema
from any_mcp.validation import validate_arguments


def test_inlines_local_refs() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"$ref": "#/$defs/A"},
            "b": {"$ref": "#/definitions/B"},
            "c": {"$ref": "#/components/schemas/C"},
        },
        "$defs": {"A": {"type": "string"}, "X": {"$ref": "#/$defs/A"}},
        "definitions": {"B": {"$ref": "#/$defs/A"}},
        "components": {"schemas": {"C": {"type": "integer"}}},
    }
    out = normalize_schema(schema)
    assert out["properties"] == {
        "a": {"type": "string"},
        "b": {"type": "string"},
        "c": {"type": "integer"},
    }
    assert "$defs" not in out and "definitions" not in out
    root = {"components": {"schemas": {"P": {"type": "boolean"}}}}
    assert normalize_schema({"$ref": "#/components/schemas/P"}, root=root) == {"type": "boolean"}


def test_cuts_recursive_refs() -> None:
    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#/$defs/Node"}},
        "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}},
    }
    out = normalize_schema(schema, max_depth=3)
    node = out["properties"]["child"]
    depth = 0
    while "properties" in node:
        node = node["properties"]["next"]
        depth += 1
    assert node == {"type": "object", "description": "(recursive, truncated)"}
    assert depth == 3


def test_rejects_external_refs() -> None:
    with pytest.raises(SchemaError, match="http://x.test/s.json") as info:
        normalize_schema({"properties": {"a": {"$ref": "http://x.test/s.json"}}})
    assert isinstance(info.value, ConfigError)
    with pytest.raises(SchemaError, match="#/\\$defs/Missing"):
        normalize_schema({"properties": {"a": {"$ref": "#/$defs/Missing"}}})


def test_root_shapes() -> None:
    assert listing_schema(None) == {"type": "object", "additionalProperties": False}
    union = {
        "anyOf": [
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a", "b"],
            },
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "c": {"type": "integer"}},
                "required": ["a"],
            },
        ]
    }
    out = listing_schema(union)
    assert out["type"] == "object"
    assert set(out["properties"]) == {"a", "b", "c"}
    assert out["required"] == ["a"]
    assert "anyOf" not in out
    with pytest.raises(SchemaError):
        listing_schema({"type": "string"})
    with pytest.raises(SchemaError):
        listing_schema({"oneOf": [{"type": "object"}, {"type": "string"}]})
    plain = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert listing_schema(plain) == plain


def test_exposed_name() -> None:
    assert exposed_name("gh", "list") == "gh_list"
    assert exposed_name("", "list") == "list"
    assert exposed_name("my ns", "a/b:c") == "my_ns_a_b_c"
    assert exposed_name("a.b", "c-d") == "a.b_c-d"
    long = exposed_name("ns", "t" * 100)
    full = "ns_" + "t" * 100
    assert len(long) == 60
    assert long.endswith("_" + hashlib.sha1(full.encode()).hexdigest()[:6])
    assert long == exposed_name("ns", "t" * 100)
    assert len(exposed_name("ns", "t" * 100, max_length=20)) == 20
    assert exposed_name("ns", "t" * 56) == "ns_" + "t" * 56  # exactly 59... fits


def test_validate_arguments_errors() -> None:
    schema = {
        "type": "object",
        "properties": {
            "b": {"type": "integer"},
            "a": {"type": "object", "properties": {"z": {"type": "string"}}},
        },
        "required": ["b", "c"],
    }
    with pytest.raises(ToolInputError) as info:
        validate_arguments(schema, {"b": "x", "a": {"z": 1}})
    paths = [p for p, _ in info.value.errors]
    assert paths == sorted(paths)
    assert set(paths) == {"$", "$.a.z", "$.b"}
    assert validate_arguments({"type": "object"}, None) == {}
    with pytest.raises(ToolInputError):
        validate_arguments({"type": "object", "required": ["q"]}, None)


def test_validate_coerces_json_strings() -> None:
    schema = {
        "type": "object",
        "properties": {
            "o": {"type": "object"},
            "l": {"type": "array", "items": {"type": "integer"}},
            "s": {"type": "string"},
            "n": {"type": "integer"},
        },
    }
    out = validate_arguments(schema, {"o": '{"k": 1}', "l": "[1, 2]", "s": '{"k": 1}'})
    assert out == {"o": {"k": 1}, "l": [1, 2], "s": '{"k": 1}'}
    with pytest.raises(ToolInputError):
        validate_arguments(schema, {"n": "5"})
    with pytest.raises(ToolInputError):
        validate_arguments(schema, {"o": "[1]"})
    with pytest.raises(ToolInputError):
        validate_arguments(schema, {"o": "not json"})


def test_validate_draft_2020_12() -> None:
    schema = {
        "type": "object",
        "properties": {
            "t": {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}]}
        },
        "dependentRequired": {"a": ["b"]},
    }
    assert validate_arguments(schema, {"t": [1, "x"], "a": 1, "b": 2}) == {
        "t": [1, "x"],
        "a": 1,
        "b": 2,
    }
    with pytest.raises(ToolInputError):
        validate_arguments(schema, {"t": ["x", 1]})
    with pytest.raises(ToolInputError):
        validate_arguments(schema, {"a": 1})
