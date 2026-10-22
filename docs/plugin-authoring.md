# Writing a plugin

An any-mcp plugin is a Python class that turns an API into MCP tools. You write only the business logic of the API; any-mcp owns validation, error mapping, tool naming, reload, response size budgets and both transports (stdio and HTTP). This guide goes from the smallest plugin to a packaged, OpenAPI-driven one. Every code block marked as an example is the real file from `examples/`, and the test suite keeps the guide and the files identical.

Every plugin imports only from `any_mcp` (and `any_mcp.openapi` for OpenAPI plugins). Test them with [the testing guide](testing-plugins.md).

## Small plugins

The smallest plugin is one class with one decorated method. Start there, and reach for the later sections only when you need them:

| You want | Use | Section |
|---|---|---|
| a handful of Python functions as tools | `@tool` methods | The hello plugin |
| one HTTP endpoint as one tool, no call code | `ToolSpec` with an `HttpBinding` | A single endpoint |
| config, auth, filtering and reshaped responses | hooks and a `Config` model | Medium plugins |
| a whole OpenAPI document, curated | `OpenAPIPlugin` | Big plugins |

### The hello plugin

<!-- example: examples/hello/hello.py -->
```python
from any_mcp import Plugin, tool


class Hello(Plugin):
    name = "hello"

    @tool
    async def greet(self, name: str) -> str:
        """Greet someone by name."""
        return f"Hello, {name}!"
```

What each part does:

- `class Hello(Plugin)` makes it a plugin. `name = "hello"` is the default namespace, so the tool is exposed as `hello_greet`.
- `@tool` marks an `async` method as a tool. The tool name is the method name, the description is the first paragraph of the docstring (a docstring is required), and the input schema is built from the signature: `name: str` becomes a required string property. A parameter with a default becomes optional.
- The return value is turned into JSON for the client. Return a string, a dict, a list, a pydantic model, a dataclass or a TypedDict. A model, TypedDict or dataclass return annotation also becomes the tool's output schema.
- `@tool(name="...", description="...", tags=[...], annotations={...})` overrides any of those.

Point a config file at it:

```yaml
# Run with: python -m any_mcp --config examples/hello/any-mcp.yaml
python_path: ["."]   # relative to this file, so hello.py is importable
plugins:
  - name: hello
    plugin: hello:Hello
```

and run `python -m any_mcp --config examples/hello/any-mcp.yaml` (stdio by default; add `--list-tools` to print the tools and exit). `python_path` entries are relative to the config file, so `hello.py` is importable. Repeat `@tool` methods to add tools; each becomes `<namespace>_<method>`.

### A single endpoint

When a tool is one HTTP request, declare the request as data. There is no call code at all: any-mcp fills the path from the arguments, sends the request with `ctx.http`, and shapes the response.

<!-- example: examples/single_endpoint/zippopotam.py -->
```python
"""One upstream endpoint exposed as one tool, declared as data with no call code."""

from any_mcp import HttpBinding, Plugin, ToolSpec


class Zippopotam(Plugin):
    name = "zippopotam"

    async def load_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="lookup_postal_code",
                description="Look up the places that belong to a postal code.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "country": {"type": "string", "description": "Country code, e.g. us"},
                        "postal_code": {"type": "string", "description": "Postal code, e.g. 90210"},
                    },
                    "required": ["country", "postal_code"],
                },
                # {country} and {postal_code} are filled from the tool arguments.
                http=HttpBinding(
                    "GET", "/{country}/{postal_code}", base_url="https://api.zippopotam.us"
                ),
            )
        ]
```

How it works:

- A plugin with no `@tool` methods overrides `load_tools` and returns a list of `ToolSpec`. It is called at start and on every reload.
- `input_schema` is a JSON Schema object for the arguments. Arguments are validated against it before your plugin sees them. (`input_model` takes a pydantic model instead.)
- `HttpBinding(method, path, ...)` describes the request. `{country}` in the path is filled from the argument of that name. Arguments named in `query=(...)` become query parameters, `headers={"arg": "Header-Name"}` maps arguments to headers, and `body` selects the JSON body: `None` (no body), `"*"` (every argument not used elsewhere), one argument name, or a tuple of names.
- `base_url` is optional. Without it the request goes to the plugin's configured `base_url` (see the next section).

The config is the same two lines as for hello, with `plugin: zippopotam:Zippopotam`.

### A decorator plugin with several tools

A decorator plugin is just the hello pattern grown up: several `@tool` methods that share state set up in `setup()`.

```python
from any_mcp import Plugin, tool


class Notes(Plugin):
    name = "notes"

    async def setup(self) -> None:
        self.notes: dict[str, str] = {}

    @tool
    async def add_note(self, key: str, text: str) -> str:
        """Store a note under a key."""
        self.notes[key] = text
        return "stored"

    @tool(name="get_note", description="Read a stored note.")
    async def read(self, key: str, default: str = "") -> str:
        return self.notes.get(key, default)
```

This exposes `notes_add_note` and `notes_get_note`. `setup()` runs once after `self.ctx` is set, and `teardown()` runs on shutdown.

## Medium plugins

Medium plugins talk to a real API with credentials and configuration. The `github_issues` example has a config model, credentials, a `before_call` guard and a `shape_response` projection.

<!-- example: examples/github_issues/github_issues.py -->
```python
"""A multi-tool plugin with auth: list and read GitHub issues, optionally comment."""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from any_mcp import Deny, Plugin, RawResponse, ToolCall, ToolResult, tool


class Config(BaseModel):
    token: str
    base_url: str = "https://api.github.com"  # also becomes ctx.http's base URL
    read_only: bool = True


class GithubIssues(Plugin):
    name = "github"
    Config = Config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.ctx.config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def credentials(self, call: ToolCall) -> Mapping[str, str]:
        return self._headers()

    async def before_call(self, call: ToolCall) -> ToolCall:
        if call.tool == "add_comment" and self.ctx.config.read_only:
            raise Deny("add_comment is disabled: the plugin is configured read_only")
        return call

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        # Tool methods call ctx.http directly, so they add the auth headers themselves.
        response = await self.ctx.http.request(method, path, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response.json()

    @tool
    async def list_issues(
        self, repo: str, state: str = "open", limit: int = 10
    ) -> list[dict[str, Any]]:
        """List issues of a repository ("owner/name"), filtered by state."""
        params = {"state": state, "per_page": limit}
        return await self._request("GET", f"/repos/{repo}/issues", params=params)

    @tool
    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        """Fetch one issue by number."""
        return await self._request("GET", f"/repos/{repo}/issues/{number}")

    @tool
    async def add_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        """Comment on an issue. Refused while the plugin is read_only."""
        path = f"/repos/{repo}/issues/{number}/comments"
        return await self._request("POST", path, json={"body": body})

    async def shape_response(self, call: ToolCall, raw: RawResponse) -> ToolResult:
        value = raw.json()
        if isinstance(value, list):
            value = [_project(item) for item in value]
        elif call.tool == "get_issue":
            value = _project(value)
        return await super().shape_response(call, RawResponse.from_value(value))


def _project(issue: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "state": issue["state"],
        "url": issue["html_url"],
    }
```

The pieces:

- **Config model.** Set `Config = Config` to a pydantic model. The `config:` mapping from the YAML is validated against it at start, and a bad config stops the server with a message that names the field. Inside the plugin it is `self.ctx.config`, an instance of your model. A field named `base_url` also becomes the base URL of `self.ctx.http`.
- **Secrets from the environment.** The config file reads `${GITHUB_TOKEN}` from the environment, so the token is never written in the file:

  ```yaml
  # Run with: GITHUB_TOKEN=... python -m any_mcp --config examples/github_issues/any-mcp.yaml
  python_path: ["."]
  plugins:
    - name: github
      plugin: github_issues:GithubIssues
      config:
        token: ${GITHUB_TOKEN}      # read from the environment, never written in the file
        read_only: true             # set false to allow add_comment
  ```

- **Credentials.** `credentials(call)` returns the outbound headers for HTTP-bound tools. Tool methods that call `self.ctx.http` directly add the headers themselves, as `_request` does.
- **`before_call`.** Runs before the tool. Return the (possibly rewritten) call, or raise `Deny("reason")` to refuse it. The message reaches the client as a denied error, and no upstream request is made.
- **`shape_response`.** Receives the `RawResponse` and returns a `ToolResult`. Reshape the data, then hand the result to `super().shape_response(...)` so the default budgeting and structured output still apply. Wrap your new value with `RawResponse.from_value(value)`.
- **Errors.** Upstream HTTP errors, validation errors and denials are mapped to error results by default; override `on_error(call, exc)` to change that. No header value ever appears in an error.

## Big plugins

When the API has an OpenAPI 3 document, do not write each tool: subclass `OpenAPIPlugin`. Every operation becomes an HTTP-bound tool, with the input schema built from the document's parameters and request body (local `$ref`s are inlined).

The petstore example is a packaged plugin that exposes three operations and projects each pet to three fields:

<!-- example: examples/petstore/src/petstore_plugin/__init__.py -->
```python
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
```

`OpenAPIConfig` is the config model; extend it to change defaults, as the example does. Its fields:

| Field | Meaning |
|---|---|
| `spec` | path or `http(s)` URL of the OpenAPI document (required) |
| `base_url` | override the document's first server URL |
| `allow` | operation ids (or glob patterns) to expose; `["*"]` exposes every operation |
| `tags` | expose the operations that carry one of these OpenAPI tags |
| `headers` | static headers sent with every request |
| `bearer_token` | sent as `Authorization: Bearer ...` |

Set `allow` or `tags`: an OpenAPI plugin with no filter refuses to start, because exposing a whole API by accident is rarely what you want. Curate the tools; an agent does better with a dozen well-chosen tools than with hundreds.

Because it is still a `Plugin`, every hook works: the example overrides `shape_response` to project the pets, and `reload_every = 300` re-reads the document every five minutes (a reload that changes the tool list is announced to clients that listen for it). The `reload_every` value is only the default; the plugin entry in the config file can override it.

## Packaging a plugin

A plugin that other people install is an ordinary Python package that advertises its class in the entry point group `any_mcp.plugins`. The petstore example's `pyproject.toml`:

```toml
[project]
name = "any-mcp-petstore"
version = "0.1.0"
description = "A packaged any-mcp plugin: a curated slice of the Petstore OpenAPI document"
requires-python = ">=3.12"
dependencies = ["any-mcp"]

[project.entry-points."any_mcp.plugins"]
petstore = "petstore_plugin:PetstorePlugin"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/petstore_plugin"]
```

Layout: `pyproject.toml` and `src/petstore_plugin/__init__.py`. Develop with an editable install so edits take effect without reinstalling:

```bash
pip install -e ./examples/petstore
```

Once installed, the config refers to the plugin by its entry point name instead of `module:Class`:

```yaml
plugins:
  - name: petstore
    plugin: petstore
```

`plugin: module:Class` still works for a plugin that is only importable through `python_path`, which is the quickest way to iterate on a single file.

## Groups of APIs

One server can host many plugins at once. Each plugin gets its own namespace, so tool names never collide, and each has its own config, HTTP client and reload schedule.

<!-- example: examples/group/any-mcp.yaml -->
```yaml
# One server for a group of APIs, each under its own namespace.
# Run with: GITHUB_TOKEN=... python -m any_mcp --config examples/group/any-mcp.yaml
python_path: ["../hello", "../github_issues", "../petstore/src"]   # relative to this file
plugins:
  - name: hello
    plugin: hello:Hello
  - name: github_issues
    namespace: gh
    plugin: github_issues:GithubIssues
    config:
      token: ${GITHUB_TOKEN}
  - name: petstore
    plugin: petstore_plugin:PetstorePlugin
```

- `name` identifies the plugin entry; `namespace` is the tool name prefix (it defaults to the plugin's `name` attribute). Here `hello_greet` comes from the first plugin and `gh_list_issues` from the second, whose namespace is `gh`.
- A failure of one plugin during a call stays inside that tool call; the others keep serving.
- `allow` on a plugin entry limits which of its tools are exposed.

## Asking the client for input

Sometimes a tool should not run until the user confirms, or needs a value the model cannot know. A plugin can pause a call and ask the client for input through one optional hook, `request_input`. The default returns `None`, so a plugin that does not override it never asks.

<!-- example: examples/confirm_delete.py -->
```python
"""A plugin that asks the client to confirm before it deletes (MCP 2026-07-28 input required).

Run it with a config next to this file:

    python_path: ["."]
    plugins:
      - name: records
        plugin: confirm_delete:ConfirmDelete

The first ``records_delete_record`` call returns an input-required result asking
``{"confirm": boolean}``. The client answers and retries the same call with
``inputResponses``; only a yes deletes. It imports only ``any_mcp``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from any_mcp import InputRequest, InputRequired, Plugin, ToolCall, ToolSpec

ID_SCHEMA = {
    "type": "object",
    "properties": {"record_id": {"type": "string"}},
    "required": ["record_id"],
    "additionalProperties": False,
}
CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {"confirm": {"type": "boolean", "title": "Delete it?"}},
    "required": ["confirm"],
}


def _confirmed(call: ToolCall) -> bool:
    """Whether the client accepted the form and answered ``confirm: true``.

    The retry must also echo the ``requestState`` this plugin issued (the record id).
    """
    if call.request_state != call.arguments["record_id"]:
        return False
    answer = call.input_responses.get("confirm")
    if not isinstance(answer, dict) or answer.get("action") != "accept":
        return False
    return (answer.get("content") or {}).get("confirm") is True


class ConfirmDelete(Plugin):
    name = "records"

    def __init__(self) -> None:
        self.records: dict[str, str] = {"r1": "first", "r2": "second"}

    async def load_tools(self) -> Sequence[ToolSpec]:
        async def delete_record(call: ToolCall) -> dict[str, Any]:
            record_id = call.arguments["record_id"]
            if not _confirmed(call):
                return {"deleted": False, "record_id": record_id}
            removed = self.records.pop(record_id, None) is not None
            return {"deleted": removed, "record_id": record_id}

        async def list_records(call: ToolCall) -> dict[str, Any]:
            return {"records": sorted(self.records)}

        return [
            ToolSpec(
                name="delete_record",
                description="Delete a record after the client confirms.",
                input_schema=ID_SCHEMA,
                handler=delete_record,
            ),
            ToolSpec(
                name="list_records",
                description="List record ids.",
                input_schema={"type": "object", "additionalProperties": False},
                handler=list_records,
            ),
        ]

    async def request_input(self, call: ToolCall) -> InputRequired | None:
        if call.tool != "delete_record" or "confirm" in call.input_responses:
            return None
        record_id = call.arguments["record_id"]
        question = InputRequest("elicitation", f"Delete record {record_id}?", CONFIRM_SCHEMA)
        return InputRequired({"confirm": question}, state=record_id)
```

How the round trip works:

1. The client calls `records_delete_record`. any-mcp calls `request_input(call)` before the tool runs. It returns an `InputRequired` holding one named `InputRequest` ("elicitation": a message and a JSON schema for the form), so the client gets an input-required result and shows the form to the user.
2. The client retries the same call, now carrying `inputResponses` (the answers, keyed by the names you chose) and the `requestState` you issued. any-mcp exposes them as `call.input_responses` and `call.request_state`.
3. `request_input` is called again. Return `None` once the answers are present, and the tool runs; return another `InputRequired` to ask more.

Things to know:

- `InputRequired(requests, state=None)`: `requests` maps a name to an `InputRequest(kind, message, schema)` and must not be empty. Today the only kind is `"elicitation"`.
- Each answer in `call.input_responses` looks like `{"action": "accept", "content": {...}}` (`action` can also be a decline or cancel), so check the action before trusting the content, as `_confirmed` does.
- **2026-07-28 clients only.** Input-required results are part of the MCP 2026-07-28 revision. A client on an older protocol revision cannot answer them, so a plugin that needs a confirmation should also have a safe fallback: in the example, an unconfirmed delete simply does not delete.
- **The state is opaque.** any-mcp passes `request_state` through verbatim and never reads it. The client can send back anything, so do not trust it: a plugin that needs tamper resistance signs or encrypts the value it puts in `InputRequired.state`. The example only uses it to check the retry belongs to the same record, and its real safety comes from checking the answer, not the state.

## Hook reference

Every hook is an `async` method on `Plugin`. Only `load_tools` is required, and only when you have no `@tool` methods; every other hook has a default, so override only what you need.

| Hook | Signature | Default |
|---|---|---|
| `setup` | `async def setup(self) -> None` | does nothing. Runs once after `self.ctx` is set; open clients and caches here |
| `teardown` | `async def teardown(self) -> None` | does nothing. Release what `setup` opened |
| `load_tools` | `async def load_tools(self) -> Sequence[ToolSpec]` | builds a `ToolSpec` from every `@tool` method. Required when there are none. Runs at start and on every reload |
| `credentials` | `async def credentials(self, call: ToolCall) -> Mapping[str, str]` | `{}`. Outbound auth headers for HTTP-bound calls |
| `before_call` | `async def before_call(self, call: ToolCall) -> ToolCall` | returns `call` unchanged. Rewrite the call, or raise `Deny` |
| `request_input` | `async def request_input(self, call: ToolCall) -> InputRequired \| None` | `None`: never asks the client for input |
| `call` | `async def call(self, call: ToolCall) -> RawResponse` | runs the tool's `handler`, or sends its `HttpBinding` with the `credentials()` headers |
| `shape_response` | `async def shape_response(self, call: ToolCall, raw: RawResponse) -> ToolResult` | the default: JSON text plus structured output, cut to the response budget |
| `on_error` | `async def on_error(self, call: ToolCall, exc: Exception) -> ToolResult` | maps denials, invalid arguments, upstream errors and timeouts to an error result |

Call order for one tool call: validate the arguments, `before_call`, `request_input`, `call` (using `credentials` for HTTP-bound tools), `shape_response`; an exception anywhere goes to `on_error`.

Class attributes: `name` (default namespace), `Config` (pydantic model for the plugin's config), `reload_every` (seconds between periodic reloads, default `None`, overridable per plugin entry in the config file) and `timeout` (seconds per call, default 30). Instance attributes: `self.ctx` (a `PluginContext` with `name`, `namespace`, `config`, `http`, `log`, `config_dir` and `request_reload()`) and `self.config` (shorthand for `self.ctx.config`).

The types you meet in hooks:

- `ToolCall`: `plugin`, `tool`, `exposed_name`, `arguments`, `spec`, `request_id`, `input_responses`, `request_state`; `call.with_arguments(new)` returns a copy with other arguments.
- `ToolSpec`: `name`, `description`, `input_schema` or `input_model`, `output_schema`, `tags`, `annotations`, and one of `handler` (an `async def handler(call) -> value`) or `http` (an `HttpBinding`).
- `RawResponse`: `status`, `headers`, `body`; `.text()`, `.json()`, and `RawResponse.from_value(value)`.
- `ToolResult`: `text`, `structured`, `is_error`.
- `Deny`: raise it from `before_call` to refuse a call.
