# Transport reference

any-mcp serves MCP over stdio (the default) or streamable HTTP. Both run the same tools through
the same pipeline; only the wire differs. There is no `sse` transport. The keys named here are
defined in the [configuration reference](configuration.md).

## stdio

`any-mcp --config any-mcp.yaml` speaks MCP on stdin and stdout. Stdout carries only MCP
messages; every log line goes to stderr. The process exits 0 when stdin closes and tears the
plugins down on SIGTERM or SIGINT. This is the right transport for a client that starts the
server itself, such as Claude Code. `--transport stdio` forces it over `server.transport`.

## Streamable HTTP

`any-mcp --config any-mcp.yaml --transport http` serves stateless streamable HTTP.

| Setting | Flag | Default | Behaviour |
|---|---|---|---|
| host: `server.http.host` | `--host` | `127.0.0.1` | Bind address. Loopback by default. |
| port: `server.http.port` | `--port` | `8000` | Bind port. |
| path: `server.http.path` | `--path` | `/mcp` | The one MCP endpoint. |
| Origin: `server.http.allowed_origins` | none | `[]` | See below. |
| bearer token: `server.http.bearer_token` | none | unset | See below. |

Flags override the file. A bad value is a config error (exit 78).

**Origin.** A request with an `Origin` header that is not a loopback origin (`localhost`,
`127.0.0.1`, `[::1]`) and not listed in `server.http.allowed_origins` gets 403. This is the MCP
rule against DNS rebinding. Requests without an `Origin` header (non-browser clients) pass this
check. List origins as written by the browser, for example `https://app.example.com`.

**Bearer token.** When `server.http.bearer_token` is set, a request without exactly
`Authorization: Bearer <token>` gets 401 with `WWW-Authenticate: Bearer`. The comparison is
constant time. Read the token from the environment with `${ANY_MCP_TOKEN}`. Binding a non-loopback
host without a token logs a stderr warning, because anyone who can reach the port can call every
tool.

The server keeps no session: no `Mcp-Session-Id`, and any instance can answer any request.

## Protocol versions

The server speaks the 2026-07-28 protocol, which has no `initialize` handshake and no sessions;
each request carries its protocol version. Clients of the earlier, handshake-based era also work
on both transports. The server declares only the `tools` capability (`listChanged: true`) in both
eras; resources, prompts and logging requests answer method-not-found. Differences between eras:

- `tools/list` results carry `ttlMs` (`tools.list_ttl_ms`) and `cacheScope`
  (`tools.cache_scope`) on 2026-07-28 only.
- An unknown tool name in `tools/call` is a JSON-RPC `-32602` error in both eras. Any other
  failure of a call is an `isError: true` result, never a protocol error.

## Tool list changes

The tool list is global, sorted, and read from the registry on every `tools/list`, so a reload
(periodic via `plugins[].reload_every`, or requested by a plugin) is visible on the next request.
Clients that cache the list for `ttlMs` see it after the TTL. On top of that the adapter sends one
change notification per real registry change, on one path per protocol era. Correctness never
depends on the notification: a client that ignores it still gets the right list on its next fetch.

The following are the probe results of the FastMCP adapter, copied from the docstring of
`src/any_mcp/_fastmcp_adapter.py` (the only module that imports FastMCP):

- Tool subclass API. `fastmcp.tools.base.Tool` takes a raw JSON-schema `parameters`
  dict plus `output_schema`, `title` and `annotations`; a subclass implements
  `async run(arguments) -> ToolResult`. A custom `fastmcp.server.providers.Provider`
  overriding `_list_tools`/`_get_tool` serves them, so FastMCP asks the registry on
  every `tools/list` and `tools/call` and a reload is visible on the next request.
  `Tool.to_mcp_tool` invents a title from the name when there is none; `_RegistryTool`
  passes its own (possibly absent) title instead. FastMCP's `ToolResult` rejects a
  non-object `structuredContent`, which MCP allows, so results are handed over as a
  prebuilt `CallToolResult` that FastMCP returns verbatim.
- Validation bypass. Only `FunctionTool` validates arguments (against its pydantic
  model); the base `Tool` passes `arguments` to `run()` untouched, so the any-mcp
  pipeline is the only validator and invalid arguments come back as `isError` results.
  `strict_input_validation=False` is set anyway and `dereference_schemas=False` keeps
  the registry's already-normalised schemas byte for byte.
- Unknown tools. FastMCP answers an unknown `tools/call` with an `isError` result;
  `_UnknownToolGuard` middleware raises `MCPError(-32602)` first, which FastMCP lets
  through to the wire as a JSON-RPC error.
- Caching. FastMCP's `cache_ttl` is whole seconds and rejects 0, so the adapter sets the
  SDK `CacheHint` on the low-level server in milliseconds: `ttlMs` equals
  `tools.list_ttl_ms` exactly. The SDK only stamps it on 2026-07-28 results.
- Capabilities. FastMCP registers resources, prompts and logging handlers and the SDK
  derives capabilities from registered handlers, so the adapter removes every handler but
  tools, discovery, ping and `subscriptions/listen` (those methods answer -32601) and
  declares `{"tools": {"listChanged": true}}` alone in both eras.
- Notifications. FastMCP only notifies inside a request (`ctx.send_notification`) and
  serves no `subscriptions/listen`; a 2026-07-28 connection has no standing channel, so
  out-of-request notifications on it are dropped. The adapter therefore sends one
  notification per real registry change on two paths, one per era:
  2026-07-28: an SDK `ListenHandler` on an `InMemorySubscriptionBus` serves
  `subscriptions/listen`; `bus.publish(ToolsListChanged())` reaches every stream that
  asked for `toolsListChanged` (a client that opened none gets nothing, per the spec).
  Handshake era: SDK middleware records each legacy `Connection` (`session._connection`,
  the same reach FastMCP's own ping middleware and stateful proxy use) until its exit stack
  closes; `Connection.send_tool_list_changed()` writes `notifications/tools/list_changed`
  on its standalone channel. Correctness never depends on either (design D8).

## Security

- **No credential forwarding.** An inbound `Authorization` or `Cookie` header is stripped before
  FastMCP sees it, tool calls carry no inbound headers, and no option forwards them. Outbound
  credentials come only from a plugin's `credentials()` hook and its `plugins[].config`.
- **Loopback by default.** HTTP binds `127.0.0.1` and accepts only loopback or allow-listed
  origins. For any other host set `server.http.bearer_token` and `server.http.allowed_origins`,
  and put TLS in front of it; any-mcp does not terminate TLS.
- **Logs.** Stderr gets one line per call (request id, tool, duration, outcome), never arguments,
  headers or tokens.

## Registering with Claude Code

Claude Code starts a stdio server itself. Register any-mcp for your user:

```bash
claude mcp add --scope user any-mcp -- any-mcp --config /abs/path/any-mcp.yaml
```

Or write a `.mcp.json` stdio entry (no `type` is needed; the default is stdio):

```json
{
  "mcpServers": {
    "any-mcp": {
      "command": "any-mcp",
      "args": ["--config", "/abs/path/any-mcp.yaml"],
      "env": { "ANY_MCP_TOKEN": "..." }
    }
  }
}
```

Or pass the file for one run with `claude --mcp-config .mcp.json` (add `--strict-mcp-config` to
ignore every other source). Servers passed with `--mcp-config` skip the project approval prompt.

A project `.mcp.json` needs approval in interactive sessions, and a committed
`.claude/settings.json` cannot grant it in an untrusted folder: `enableAllProjectMcpServers` and
`enabledMcpjsonServers` are honoured there only from user settings, managed settings or
`--settings`. Use user scope or `--mcp-config` for unattended setups.

To reach an HTTP instance instead, run `any-mcp --transport http` and add it with
`claude mcp add --transport http any-mcp http://127.0.0.1:8000/mcp`, adding
`--header "Authorization: Bearer $ANY_MCP_TOKEN"` when a bearer token is set.
