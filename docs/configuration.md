# Configuration reference

any-mcp reads one config file. Every key below is written as its dotted path; `plugins[]` means
"each entry of the `plugins` list".

## File format

A YAML or JSON file (JSON is valid YAML, so one parser reads both), chosen with
`--config <path>` or the `ANY_MCP_CONFIG` environment variable. The top level must be a mapping.
An empty file is an empty config. Unknown keys are rejected, so a typo fails at startup instead
of being ignored. Relative paths in the file resolve against the config file's directory.

```yaml
server:
  transport: stdio
tools:
  response_budget_chars: 20000
python_path: [./plugins]
plugins:
  - name: hello
    plugin: hello_plugin:Hello
```

Without a file, `--plugin REF` (repeatable) builds a default config with one entry per reference;
with a file, `--plugin` entries are appended to `plugins[]`. Each entry is named after the
class (the part after the colon, lower-cased) or the reference itself. Pass at least one of
`--config`, `--plugin` or `ANY_MCP_CONFIG`.

### Command line

| Flag | Meaning |
|---|---|
| `--config PATH` | The config file. Defaults to `$ANY_MCP_CONFIG`. |
| `--plugin REF` | A plugin as `module:Class` or entry-point name. Repeatable. |
| `--transport {stdio,http}` | Overrides `server.transport`. See [transports](transports.md). |
| `--host HOST` | Overrides `server.http.host`. |
| `--port PORT` | Overrides `server.http.port`. |
| `--path PATH` | Overrides `server.http.path`. |
| `--insecure` | Sets `server.http.insecure`: allow a non-loopback HTTP bind without a bearer token. |
| `--log-level {debug,info,warning,error}` | Overrides `server.log_level`. |
| `--list-tools` | Load the plugins, print the exposed tools as a JSON array on stdout, exit 0. |
| `--version` | Print the version and exit. |

## Environment variables

`${VAR}` and `${VAR:-default}` expand in every string value of the file, including inside lists
and inside `plugins[].config`. `${VAR}` with `VAR` unset and no default is a config error naming
the key. `${VAR:-}` expands to the empty string when unset. Expansion happens on the parsed
values, so a secret never needs to be written into the file:

```yaml
server:
  http:
    bearer_token: ${ANY_MCP_TOKEN}
```

`ANY_MCP_CONFIG` is the only variable any-mcp itself reads: it is the default of `--config`.

## `server`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `server` | mapping | all defaults | Server identity, transport and logging. |
| `server.name` | string | `any-mcp` | Server name announced to clients. |
| `server.instructions` | string or null | null | Instructions text announced to clients. |
| `server.transport` | `stdio` or `http` | `stdio` | Transport; `--transport` overrides it. |
| `server.log_level` | `debug`, `info`, `warning`, `error` | `info` | Level of the stderr log; `--log-level` overrides it. |
| `server.http` | mapping | all defaults | Settings used only with the `http` transport. |
| `server.http.host` | string | `127.0.0.1` | Bind address; `--host` overrides it. |
| `server.http.port` | integer 0-65535 | `8000` | Bind port, 0 picks a free one; `--port` overrides it. |
| `server.http.path` | string | `/mcp` | Endpoint path; `--path` overrides it. |
| `server.http.bearer_token` | string or null | null | When set, requests need `Authorization: Bearer <token>`. Must not be empty; unset it to disable auth. |
| `server.http.insecure` | boolean | `false` | Allow binding a non-loopback host with no `bearer_token` (a warning is logged); otherwise that start is refused with exit 78. `--insecure` sets it. |
| `server.http.allowed_origins` | list of strings | `[]` | `Origin` values accepted in addition to loopback origins. |

Logs always go to stderr, never stdout. See [transports](transports.md) for how the HTTP keys behave.

## `tools`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `tools` | mapping | all defaults | How the tool list is built and served. |
| `tools.max_name_length` | integer 16-128 | `60` | Longest exposed tool name; longer names are truncated with a stable `_<6 hex>` suffix. |
| `tools.list_ttl_ms` | integer >= 0 | `30000` | `ttlMs` on `tools/list` results of the 2026-07-28 protocol. |
| `tools.cache_scope` | `public` or `private` | `public` | `cacheScope` on `tools/list` results. |
| `tools.response_budget_chars` | integer >= 1 | `40000` | Character budget for one tool result before it is shaped down. |

Exposed names are `<namespace>_<tool>`, sorted, and the same for every connection.

## `plugins`

A config file is trusted like code. Plugin references (`module:Class`, entries added through `python_path`, and entry-point names) import operator-supplied code into the server process, which runs it with the server's rights. Only point `plugins[].plugin` and `python_path` at code you trust, and keep the config file writable by the operator alone.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `python_path` | list of strings | `[]` | Directories added to the import path for `module:Class` plugins; relative entries resolve against the config file. |
| `plugins` | list of mappings | `[]` | The plugins to load, in order. Names must be unique. |
| `plugins[].name` | string | required | Unique id, `[a-z][a-z0-9_-]{0,19}`; the default namespace. |
| `plugins[].plugin` | string | required | `module:Class` or the name of an installed entry point. |
| `plugins[].namespace` | string or null | the entry `name` | Prefix of the plugin's tool names; `""` means no prefix. |
| `plugins[].allow` | list of strings | `[]` (all) | Globs on plugin-local tool names; only matching tools are exposed. |
| `plugins[].tags` | list of strings | `[]` (all) | Only tools carrying one of these tags are exposed. |
| `plugins[].timeout` | number > 0 or null | the plugin's `timeout` (30 s) | Seconds one tool call may take. |
| `plugins[].reload_every` | number > 0 or null | the plugin's `reload_every` (never) | Seconds between periodic reloads of the tool list. |
| `plugins[].config` | mapping | `{}` | Passed to the plugin and validated by its `Config` model; `${VAR}` expands here too. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean exit: stdin closed (stdio), SIGTERM or SIGINT, or `--list-tools` finished. |
| `78` | Configuration error: no config given, unreadable or invalid file, an unset `${VAR}`, an invalid flag value, or a plugin that fails to load, in `setup()` or in its first `load_tools()`. One line on stderr names the key or plugin. |
| `2` | Command line usage error (argparse). |

An uncaught internal error ends the process with Python's own non-zero status and a traceback
on stderr.
