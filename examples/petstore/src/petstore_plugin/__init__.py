"""A packaged OpenAPI plugin: three Petstore operations, with pets projected to three fields."""

from importlib.resources import files
from typing import Any

from pydantic import Field

from any_mcp import RawResponse, ToolCall, ToolResult
from any_mcp.openapi import OpenAPIConfig, OpenAPIPlugin

__all__ = ["PetstorePlugin"]

FIELDS = ("id", "name", "status")


class Config(OpenAPIConfig):
    spec: str = str(files("petstore_plugin") / "petstore.yaml")
    allow: list[str] = Field(default_factory=lambda: ["listPets", "getPet", "createPet"])


def _project(value: Any) -> Any:
    if isinstance(value, list):
        return [_project(item) for item in value]
    if isinstance(value, dict):
        return {key: value[key] for key in FIELDS if key in value}
    return value


class PetstorePlugin(OpenAPIPlugin):
    name = "petstore"
    Config = Config
    reload_every = 300

    async def shape_response(self, call: ToolCall, raw: RawResponse) -> ToolResult:
        if raw.status >= 400:
            return await super().shape_response(call, raw)
        try:
            projected = _project(raw.json())
        except ValueError:
            return await super().shape_response(call, raw)
        return await super().shape_response(call, RawResponse.from_value(projected))
