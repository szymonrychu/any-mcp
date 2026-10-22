from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from any_mcp.context import PluginContext
from any_mcp.errors import ConfigError
from any_mcp.openapi import OpenAPIPlugin
from any_mcp.testing import PluginHarness
from any_mcp.types import ToolCall, ToolSpec

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"
PETSTORE = (FIXTURES / "petstore.yaml").read_text()
RECURSIVE = (FIXTURES / "recursive.json").read_text()


def make_plugin(
    cls: type[OpenAPIPlugin] = OpenAPIPlugin,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    config_dir: Path | None = FIXTURES,
    **config: Any,
) -> OpenAPIPlugin:
    plugin = cls()
    plugin.ctx = PluginContext(
        name="api",
        namespace="api",
        config=cls.Config.model_validate(config),
        http=httpx.AsyncClient(transport=transport),
        log=logging.getLogger("test"),
        response_budget_chars=40000,
        config_dir=config_dir,
    )
    return plugin


def a_call(spec: ToolSpec) -> ToolCall:
    return ToolCall(
        plugin="api",
        tool=spec.name,
        exposed_name=spec.name,
        arguments={},
        spec=spec,
        request_id="1",
    )


async def load(**config: Any) -> dict[str, ToolSpec]:
    specs = await make_plugin(**config).load_tools()
    return {s.name: s for s in specs}


async def test_loads_specs_from_file_and_url() -> None:
    yaml_tools = await load(spec="petstore.yaml", allow=["*"])
    assert set(yaml_tools) == {"listPets", "createPet", "get_pets_petId", "deletePet", "get_health"}
    json_tools = await load(spec="recursive.json", allow=["*"], base_url="https://x.test")
    assert set(json_tools) == {"addNode"}

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=PETSTORE)

    plugin = make_plugin(
        transport=httpx.MockTransport(handler), spec="https://specs.test/p.yaml", allow=["listPets"]
    )
    specs = await plugin.load_tools()
    assert [s.name for s in specs] == ["listPets"]
    assert seen == ["https://specs.test/p.yaml"]

    bad = make_plugin(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
        spec="https://specs.test/p.yaml",
        allow=["*"],
    )
    with pytest.raises(ConfigError, match="specs.test"):
        await bad.load_tools()
    with pytest.raises(ConfigError, match="nope.yaml"):
        await make_plugin(spec="nope.yaml", allow=["*"]).load_tools()


async def test_selection_requires_filter() -> None:
    with pytest.raises(ConfigError) as err:
        await load(spec="petstore.yaml")
    assert (
        'set allow or tags in the openapi plugin config (allow: ["*"] exposes every operation)'
        in str(err.value)
    )
    assert set(await load(spec="petstore.yaml", allow=["*Pet"])) == {"createPet", "deletePet"}
    assert set(await load(spec="petstore.yaml", tags=["admin"])) == {"deletePet"}
    assert set(await load(spec="petstore.yaml", allow=["listPets"], tags=["write"])) == {
        "listPets",
        "createPet",
    }


async def test_tool_names_and_descriptions() -> None:
    tools = await load(spec="petstore.yaml", allow=["*"])
    assert tools["listPets"].description == "List pets\n\nReturns pets, newest first."
    assert tools["createPet"].description == "Create a pet"
    assert tools["get_pets_petId"].description == "Fetch one pet"
    assert tools["deletePet"].description == "DELETE /pets/{petId}"
    assert tools["get_health"].description == "GET /health"
    assert tools["createPet"].tags == frozenset({"pets", "write"})


async def test_parameter_and_body_mapping() -> None:
    tools = await load(spec="petstore.yaml", allow=["*"])
    listing = tools["listPets"]
    schema = listing.json_schema()
    assert set(schema["properties"]) == {"limit", "status", "X-Trace"}
    assert schema["properties"]["limit"] == {
        "type": "integer",
        "description": "Maximum number of pets",
    }
    assert schema["properties"]["status"]["enum"] == ["available", "sold"]
    assert "required" not in schema
    assert listing.http is not None
    assert listing.http.method == "GET"
    assert listing.http.path == "/pets"
    assert listing.http.query == ("limit", "status")
    assert dict(listing.http.headers) == {"X-Trace": "X-Trace"}
    assert listing.http.body is None
    assert listing.http.base_url == "https://petstore.test/v1"

    create = tools["createPet"]
    cschema = create.json_schema()
    assert cschema["required"] == ["body"]
    assert cschema["properties"]["body"]["properties"]["owner"]["properties"]["id"] == {
        "type": "integer"
    }
    assert create.http is not None
    assert create.http.body == "body"

    delete = tools["deletePet"].json_schema()
    assert delete["required"] == ["petId", "X-Confirm"]
    assert tools["get_pets_petId"].http is not None
    assert tools["get_pets_petId"].http.path == "/pets/{petId}"


async def test_ref_handling() -> None:
    tools = await load(spec="recursive.json", allow=["*"], base_url="https://x.test")
    body = tools["addNode"].json_schema()["properties"]["body"]
    assert "$ref" not in json.dumps(body)
    assert body["properties"]["children"]["type"] == "array"
    assert "required" not in tools["addNode"].json_schema()

    external = {
        "openapi": "3.0.0",
        "servers": [{"url": "https://x.test"}],
        "paths": {
            "/a": {
                "get": {
                    "operationId": "a",
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"$ref": "other.yaml#/Thing"}}
                    ],
                }
            }
        },
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=external))
    plugin = make_plugin(transport=transport, spec="https://s.test/s.json", allow=["*"])
    with pytest.raises(ConfigError, match="other.yaml#/Thing"):
        await plugin.load_tools()


async def test_swagger2_rejected() -> None:
    doc = {"swagger": "2.0", "info": {"title": "old", "version": "1"}, "paths": {}}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=doc))
    plugin = make_plugin(transport=transport, spec="https://s.test/s.json", allow=["*"])
    with pytest.raises(ConfigError, match="only OpenAPI 3"):
        await plugin.load_tools()


async def test_base_url_and_credentials() -> None:
    tools = await load(spec="petstore.yaml", allow=["listPets"], base_url="https://override.test")
    assert tools["listPets"].http is not None
    assert tools["listPets"].http.base_url == "https://override.test"

    # relative server url resolves against the spec url
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=RECURSIVE))
    plugin = make_plugin(transport=transport, spec="https://s.test/dir/spec.json", allow=["*"])
    [spec] = await plugin.load_tools()
    assert spec.http is not None
    assert spec.http.base_url == "https://s.test/api"

    # no servers, no base_url
    with pytest.raises(ConfigError, match="base_url"):
        await load(spec="recursive.json", allow=["*"])

    secured = make_plugin(
        spec="petstore.yaml", headers={"X-Api": "k"}, bearer_token="tok", allow=["*"]
    )
    call = a_call((await secured.load_tools())[0])
    assert await secured.credentials(call) == {"X-Api": "k", "Authorization": "Bearer tok"}
    plain = make_plugin(spec="petstore.yaml", allow=["*"])
    assert await plain.credentials(call) == {}


async def test_subclass_defaults() -> None:
    class Petstore(OpenAPIPlugin):
        name = "petstore"

        class Config(OpenAPIPlugin.Config):
            spec: str = "petstore.yaml"
            allow: list[str] = ["listPets"]

        async def credentials(self, call: ToolCall) -> dict[str, str]:
            return {"X-Sub": "1"}

    plugin = make_plugin(Petstore)
    assert [s.name for s in await plugin.load_tools()] == ["listPets"]
    assert await plugin.credentials(a_call((await plugin.load_tools())[0])) == {"X-Sub": "1"}


async def test_end_to_end_with_harness() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"id": 1}, {"id": 2}])

    async with PluginHarness(
        OpenAPIPlugin,
        config={
            "spec": str(FIXTURES / "petstore.yaml"),
            "allow": ["listPets"],
            "bearer_token": "tok",
        },
        name="pets",
        transport=httpx.MockTransport(handler),
    ) as harness:
        result = await harness.call("listPets", {"limit": 2})
    assert not result.is_error
    assert result.structured == [{"id": 1}, {"id": 2}] or result.structured == {
        "result": [{"id": 1}, {"id": 2}]
    }
    [request] = requests
    assert request.method == "GET"
    assert str(request.url) == "https://petstore.test/v1/pets?limit=2"
    assert request.headers["authorization"] == "Bearer tok"


async def test_relative_spec_resolves_against_config_file(tmp_path: Path) -> None:
    (tmp_path / "petstore.yaml").write_text(PETSTORE)
    (tmp_path / "any-mcp.yaml").write_text(
        "plugins:\n  - name: pets\n    plugin: any_mcp.openapi:OpenAPIPlugin\n"
        "    config:\n      spec: petstore.yaml\n      allow: ['listPets']\n"
    )
    async with PluginHarness.from_config(tmp_path / "any-mcp.yaml") as harness:
        names = [t["name"] for t in await harness.list_tools()]
    assert names == ["pets_listPets"]
