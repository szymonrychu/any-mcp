# Concepts

any-mcp is an MCP-to-API proxy that does nothing on its own: plugins say which tools exist and how
to call the API behind them, and the core runs everything around that. This page explains the
moving parts. To build a plugin, read [Writing a plugin](plugin-authoring.md); to look up a key,
read the [configuration reference](configuration.md).

## Plugins

A plugin is a subclass of `Plugin` (an abstract base class). It gives the server a set of tools.
There are two ways to do that, and you can mix them:

- Decorate `async` methods with `@tool`. The method name becomes the tool name, the first
  paragraph of the docstring the description, the signature the input schema and a model,
  `TypedDict` or dataclass return annotation the output schema.
- Override `load_tools` and return `ToolSpec` objects yourself. A tool can run Python (a
  `handler`) or describe an HTTP request (an `HttpBinding`) that the core sends for you.

A plugin is named in the config file as `module:Class` (importable through `python_path`) or as
the name of an installed entry point. One server can load many plugins at once, each with its
own namespace, config, HTTP client and reload schedule. The core is what keeps plugins small:
validation, error mapping, tool naming, reload, response budgets and both transports are not the
plugin's job.

## Hooks

Every hook is an `async` method on `Plugin`. Only `load_tools` can be required, and only when you
have no `@tool` methods. Everything else has a default, so override only what you need.

- `setup`: default does nothing. It runs once after `self.ctx` is set, before the first
  `load_tools`. Override it to open something you will reuse, such as a client, a cache or a
  login session, and read your settings from `self.config`.
- `teardown`: default does nothing. It runs when the server stops. Override it to release
  whatever `setup` opened. Plugins are torn down in reverse order of loading.
- `load_tools`: default builds one tool from every `@tool` method. It runs at start and on every
  reload. Override it when the tools are not known until runtime, for example when they come
  from an OpenAPI document or from a list the API returns.
- `credentials`: default returns no headers. It supplies the outbound auth headers for tools that
  use an HTTP binding. Override it to add an API key, a bearer token or a signed header. It
  runs per call, so it can refresh a token.
- `before_call`: default returns the call unchanged. It runs after the arguments are validated.
  Override it to rewrite the arguments, to apply a policy such as read-only mode, or to raise
  `Deny` to refuse the call; the denial message is shown to the client.
- `request_input`: default returns `None`, so a call never asks the client for more input.
  Override it for a call that needs a confirmation or a missing value: return an
  `InputRequired` and the client answers and calls again with its answers in
  `call.input_responses`.
- `call`: default runs the tool's handler, or sends its HTTP binding with the `credentials`
  headers. Override it to wrap every call, for example to add retries or tracing, and delegate
  to `super().call(call)` for the real work.
- `shape_response`: default turns the raw response into JSON text plus structured output, cut to
  the response budget. Override it to project fields, reformat text, or choose what the agent
  sees from a large reply.
- `on_error`: default maps denials, invalid arguments, upstream errors and timeouts to an error
  result and hides the details of any other exception. Override it to word errors for your API
  or to hide more.

The signatures and the attributes a hook can read (`ToolCall`, `ToolSpec`, `RawResponse`,
`ToolResult`) are in the [hook reference](plugin-authoring.md#hook-reference). The class
attributes `name`, `Config`, `reload_every` and `timeout` set the default namespace, the plugin's
config model, the periodic reload interval and the seconds one call may take.

## Call pipeline

Every tool call runs the same steps in the same order, whichever transport carried it:

1. The core finds the tool by its exposed name. An unknown name is a protocol error.
2. The arguments are validated against the tool's input schema. Invalid arguments never reach
   plugin code.
3. `before_call` runs and may rewrite the call or raise `Deny`.
4. `request_input` runs. If it returns an `InputRequired`, the call stops here and the client is
   asked; otherwise the call goes on.
5. `call` runs under the plugin's `timeout`: the handler, or the HTTP binding sent with the
   `credentials` headers.
6. `shape_response` turns the raw response into the tool result, within the response budget.

An exception at any step goes to `on_error`, and its result is returned to the client as an error
result. Only cancellation escapes. Each call writes one stderr line with the request id, the
tool, the duration and the outcome.

## Reload

The tool list is not fixed at start. `load_tools` runs again on a reload, which happens in three
ways: periodically when `reload_every` is set (on the class or on the plugin entry in the config
file, where the entry wins), on demand when the plugin calls `self.ctx.request_reload()`, and in
tests through the harness.

A reload is safe. If a plugin's `load_tools` raises, or the new tools are invalid or collide with
another plugin's names, that plugin keeps its previous tools and one error line is logged. The
tool list stays global and sorted, so the next request already sees the new tools. Clients
that subscribed to tool-list changes are told only when the list really changed. How each
protocol era delivers that is described in the [transport reference](transports.md#tool-list-changes).

## Tool names

A client sees `<namespace>_<tool>`. The namespace is the plugin entry's `namespace`, which
defaults to its `name`; set it to `""` for no prefix. Characters outside letters, digits, `_`, `.`
and `-` become `_`. Names longer than `tools.max_name_length` (default 60) are cut and end in a
stable `_<6 hex>` suffix, so the same tool always gets the same name. Names are sorted and the
same for every connection.

Two tools that end up with the same exposed name are an error: at start the server refuses to
run, and on reload the offending plugin keeps its old tools. Give one of them a namespace.
Use `plugins[].allow` (globs on the plugin-local tool names) and `plugins[].tags` to expose only
part of a plugin.

## Errors

A failed call is a tool result with `is_error` set and a structured `error` object holding a
`kind` and a `message`. The default mapping is:

| Kind | When |
|---|---|
| `denied` | a hook raised `Deny`; the message is the plugin's own |
| `invalid_arguments` | the arguments failed validation; the message names each bad path |
| `upstream` | the API answered with an error status; it carries `status` and the start of the body |
| `timeout` | the call ran longer than the plugin's `timeout` |
| `internal` | anything else; only the plugin name and the exception type are shown |

An unexpected exception never leaks its message, a stack trace or a header value to the client.
Override `on_error` to change any of this. Invalid configuration is different: it stops the
server at startup with a one-line message and exit code 78 (see the
[exit codes](configuration.md#exit-codes)).

## Security

- **Outbound credentials stay in the plugin.** They come only from the `credentials` hook and
  from `plugins[].config`, where `${VAR}` reads them from the environment so the file holds no
  secret. Inbound `Authorization` and `Cookie` headers are never forwarded to the API.
- **Errors and logs are scrubbed.** No error result and no log line contains a header value,
  and the per-call log line never contains arguments.
- **Loopback by default.** The HTTP transport binds `127.0.0.1` and accepts only loopback or
  allow-listed origins. For any other host set a bearer token and the allowed origins, and
  terminate TLS at a reverse proxy; any-mcp serves plain HTTP only. See the
  [transport reference](transports.md#security).
- **Input is validated before plugin code runs**, and `before_call` is the place for policy such
  as a read-only switch.
- **Plugins are trusted code.** A plugin runs in the server process with the server's rights,
  so install only plugins you trust.
