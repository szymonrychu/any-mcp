"""The built-in OpenAPI plugin: an OpenAPI 3 document becomes a curated set of HTTP-bound tools."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from any_mcp.errors import ConfigError
from any_mcp.plugin import Plugin
from any_mcp.schema import normalize_schema
from any_mcp.types import HttpBinding, ToolCall, ToolSpec

__all__ = ["OpenAPIPlugin"]

_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
_PARAM_LOCATIONS = ("path", "query", "header")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_SERVER_VAR = re.compile(r"\{([^{}]+)\}")
_JSON = "application/json"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_ABSOLUTE_PATH = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_NO_FILTER = 'set allow or tags in the openapi plugin config (allow: ["*"] exposes every operation)'


class OpenAPIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: str
    base_url: str | None = None
    allow: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    bearer_token: str | None = None


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), port


def _resolve(node: Any, doc: dict[str, Any]) -> Any:
    """Inline local ``$ref``s in ``node`` (cycles cut); an external ref raises ConfigError."""
    if isinstance(node, dict):
        return normalize_schema(node, root=doc)
    return node


def _operation_name(method: str, path: str, operation: Mapping[str, Any]) -> str:
    op_id = operation.get("operationId")
    if isinstance(op_id, str) and op_id.strip():
        return op_id
    return f"{method}_{_NON_ALNUM.sub('_', path).strip('_')}"


def _description(method: str, path: str, operation: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("summary", "description"):
        value = operation.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    return "\n\n".join(parts) or f"{method.upper()} {path}"


class OpenAPIPlugin(Plugin):
    """Expose a selected subset of an OpenAPI 3 document's operations as tools."""

    name = "openapi"
    Config = OpenAPIConfig
    _abstract = False

    @property
    def cfg(self) -> OpenAPIConfig:
        config = self.ctx.config
        assert isinstance(config, OpenAPIConfig)
        return config

    async def credentials(self, call: ToolCall) -> Mapping[str, str]:
        out = dict(self.cfg.headers)
        if self.cfg.bearer_token:
            out["Authorization"] = f"Bearer {self.cfg.bearer_token}"
        return out

    async def _read_spec(self) -> tuple[str, str]:
        """The spec text and the location it came from (a URL or a file path)."""
        spec = self.cfg.spec
        if _is_url(spec):
            try:
                response = await self.ctx.http.get(spec)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ConfigError(f"openapi spec {spec}: cannot fetch: {exc}") from exc
            return response.text, spec
        path = Path(spec)
        if not path.is_absolute():
            path = (self.ctx.config_dir or Path.cwd()) / path
        try:
            return path.read_text(encoding="utf-8"), str(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"openapi spec {path}: cannot read: {exc}") from exc

    async def _document(self) -> tuple[dict[str, Any], str]:
        text, source = await self._read_spec()
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"openapi spec {source}: not valid JSON or YAML: {exc}") from exc
        if not isinstance(doc, dict):
            raise ConfigError(f"openapi spec {source}: top level must be a mapping")
        version = doc.get("openapi")
        if not (isinstance(version, str) and version.startswith("3.")):
            what = f"swagger {doc['swagger']}" if "swagger" in doc else "an unknown format"
            raise ConfigError(
                f"openapi spec {source}: only OpenAPI 3 is supported (this document is {what})"
            )
        return doc, source

    def _base_url(self, doc: Mapping[str, Any], source: str) -> str:
        if self.cfg.base_url:
            return self.cfg.base_url
        servers = doc.get("servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            url = servers[0].get("url")
            if isinstance(url, str) and url:
                variables = servers[0].get("variables") or {}
                url = _SERVER_VAR.sub(
                    lambda m: str((variables.get(m.group(1)) or {}).get("default", m.group(0))),
                    url,
                )
                if _is_url(source):
                    resolved = urljoin(source, url)
                    if _origin(resolved) == _origin(source):
                        return resolved
                elif _is_url(url):
                    return url
        raise ConfigError(
            f"openapi spec {source}: no usable server url (a spec fetched by url may only name "
            "its own origin; a local spec needs an http(s) url); set base_url in the openapi "
            "plugin config"
        )

    def _selected(self, name: str, tags: Sequence[str]) -> bool:
        cfg = self.cfg
        return any(fnmatchcase(name, pattern) for pattern in cfg.allow) or any(
            tag in cfg.tags for tag in tags
        )

    async def load_tools(self) -> Sequence[ToolSpec]:
        if not self.cfg.allow and not self.cfg.tags:
            raise ConfigError(_NO_FILTER)
        doc, source = await self._document()
        paths = doc.get("paths")
        if not isinstance(paths, dict):
            raise ConfigError(f"openapi spec {source}: no paths")
        specs: list[ToolSpec] = []
        selected: list[tuple[str, str, dict[str, Any], list[Any]]] = []
        for path, item in paths.items():
            item = _resolve(item, doc) if isinstance(item, dict) and "$ref" in item else item
            if not isinstance(item, dict):
                continue
            shared = item.get("parameters") or []
            for method in _METHODS:
                operation = item.get(method)
                if not isinstance(operation, dict):
                    continue
                tags = [t for t in operation.get("tags") or [] if isinstance(t, str)]
                if self._selected(_operation_name(method, path, operation), tags):
                    selected.append((method, path, operation, shared))
        if not selected:
            return specs
        for _, path, _, _ in selected:
            if _ABSOLUTE_PATH.match(path):
                raise ConfigError(
                    f"openapi spec {source}: absolute url path {path!r} refused; paths must be "
                    "relative to the configured host"
                )
        base_url = self._base_url(doc, source)
        for method, path, operation, shared in selected:
            specs.append(self._tool(doc, method, path, operation, shared, base_url))
        return specs

    def _tool(
        self,
        doc: dict[str, Any],
        method: str,
        path: str,
        operation: dict[str, Any],
        shared: list[Any],
        base_url: str,
    ) -> ToolSpec:
        properties: dict[str, Any] = {}
        required: list[str] = []
        query: list[str] = []
        headers: dict[str, str] = {}

        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in [*shared, *(operation.get("parameters") or [])]:
            param = _resolve(raw, doc)
            if not isinstance(param, dict) or not param.get("name"):
                continue
            if param.get("in") in _PARAM_LOCATIONS:
                merged[(param["in"], param["name"])] = param
        for (location, pname), param in merged.items():
            schema = dict(param.get("schema") or {})
            if param.get("description") and "description" not in schema:
                schema["description"] = param["description"]
            properties[pname] = schema
            if param.get("required") or location == "path":
                required.append(pname)
            if location == "query":
                query.append(pname)
            elif location == "header":
                headers[pname] = pname

        body: str | None = None
        request_body = operation.get("requestBody")
        if isinstance(request_body, dict):
            request_body = _resolve(request_body, doc)
            media = (request_body.get("content") or {}).get(_JSON)
            if isinstance(media, dict):
                schema = dict(media.get("schema") or {})
                if request_body.get("description") and "description" not in schema:
                    schema["description"] = request_body["description"]
                properties["body"] = schema
                body = "body"
                if request_body.get("required"):
                    required.append("body")

        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            input_schema["required"] = required
        return ToolSpec(
            name=_operation_name(method, path, operation),
            description=_description(method, path, operation),
            input_schema=input_schema,
            tags=frozenset(t for t in operation.get("tags") or [] if isinstance(t, str)),
            http=HttpBinding(
                method.upper(),
                path,
                query=tuple(query),
                headers=headers,
                body=body,
                base_url=base_url,
            ),
        )
