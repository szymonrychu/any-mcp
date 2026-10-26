# any-mcp

## What it is

any-mcp is an MCP-to-API proxy: a Python MCP server that exposes any API, or a group of APIs, as MCP tools. It does nothing on its own. Plugins provide the business logic: a plugin is one Python class that says which tools exist and how to call the API behind them, and any-mcp owns everything else (argument validation, error mapping, tool naming, reload, response size budgets, and both the stdio and the Streamable HTTP transport).

## Installation

any-mcp is a normal Python package (Python >= 3.12). Installing it puts one command on `PATH`: `any-mcp`.

Index forms (marked "index form" below) need a package index: once published to an index; until then use the git forms.

```bash
pip install any-mcp                                     # from a package index (index form)
pip install git+https://github.com/szymonrychu/any-mcp   # straight from git
pip install "any-mcp @ git+https://github.com/szymonrychu/any-mcp@v0.4.0"   # pinned to a release tag
pip install .                                           # from a checkout
pipx install any-mcp                                    # isolated, on PATH (index form)
uv tool install any-mcp                                 # same, with uv (index form)

any-mcp --version
```

### Agent hook install

An agent image or an onStart hook can install any-mcp into its own virtual environment with plain `venv` and `pip` (no uv needed), pinned to the release tag:

```bash
python -m venv /opt/any-mcp && /opt/any-mcp/bin/pip install "any-mcp @ git+https://github.com/szymonrychu/any-mcp@v0.4.0"
```

The console script is then `/opt/any-mcp/bin/any-mcp`. The hook can write the config file with the standard library only, as JSON: JSON is valid YAML, so the config file may be JSON (see [Configuration](docs/configuration.md#file-format)).

## Quickstart

A plugin is a class with `@tool` methods. Save this as `hello.py`:

```python
from any_mcp import Plugin, tool


class Hello(Plugin):
    name = "hello"

    @tool
    async def greet(self, name: str) -> str:
        """Greet someone by name."""
        return f"Hello, {name}!"
```

Point a config file at it, as `any-mcp.yaml` in the same directory:

```yaml
python_path: ["."]   # relative to this file, so hello.py is importable
plugins:
  - name: hello
    plugin: hello:Hello
```

Run the server (it speaks MCP over stdio by default):

```bash
any-mcp --config any-mcp.yaml
```

Register it with Claude Code, which starts the stdio server itself:

```bash
claude mcp add --scope user hello -- any-mcp --config /abs/path/any-mcp.yaml
```

The agent now sees one tool, `hello_greet`. The tool name is `<namespace>_<tool>`; see [Tool names](docs/concepts.md#tool-names).

## Orchestrator plugin

`any_mcp.orchestrator:OrchestratorPlugin` is built into any-mcp (like `any_mcp.openapi:OpenAPIPlugin`). It is the one plugin through which agents call the agents-orchestrator agent API `/agent/v1`. It does no network I/O at start; every tool is a thin HTTP call that returns the server JSON, and a non-2xx answer becomes a tool error carrying the server's `error.code` and message (`not_leader`, `stale_epoch`, `no_peers`, `alone`, `static_session`, `out_of_range`, `leader_would_leave`, `invalid_path`, `target_not_superleader`, `hop_limit`, `pair_budget`, ...).

Config fields (extra keys are refused): `base_url`, `token`, `session` (session key), `agent` (pod name), `slug`, `max_replicas` (default 1), `static` (default false), `hive` (default false), `auto_compact_pct` (default null). Every request carries `Authorization: Bearer <token>` and `X-Orch-Session: <session>`; the token is never logged or echoed in a result or error.

| Tool | Route |
|------|-------|
| `orchestrator_me` | `GET /agent/v1/me` |
| `orchestrator_leadership_handover(reason, epoch, to?)` | `POST /agent/v1/leadership/handover` |
| `orchestrator_lock_claim(paths)` | `POST /agent/v1/locks/claim` |
| `orchestrator_lock_release(paths)` | `POST /agent/v1/locks/release` |
| `orchestrator_lock_list` | `GET /agent/v1/locks` |
| `orchestrator_lock_reassign(path, to?)` | `POST /agent/v1/locks/reassign` (`to` null discards the lock) |
| `orchestrator_scale(replicas)` | `POST /agent/v1/scale` |
| `orchestrator_init_scripts_get` | `GET /agent/v1/init-scripts` |
| `orchestrator_init_scripts_edit(scripts)` | `PUT /agent/v1/init-scripts` |
| `orchestrator_sessions_list` | `GET /agent/v1/sessions` |
| `orchestrator_session_send(slug, text)` | `POST /agent/v1/sessions/{slug}/messages` (body `{"text"}`; asynchronous, answers a message id) |

The plugin always lists all eleven tools. Which an agent sees is decided by the `allow` list of the entry the orchestrator renders (`scale` only on a scalable session, the hive tools only in a hive). The session tools `sessions_list` and `session_send` are rendered for every session; the orchestrator decides what each session may list and send: a superleader session's leader lists and messages every other session, any other session's leader lists and messages only the superleader sessions. A message arrives at the target leader as a new turn framed `<session-message from="<slug>" ...>`; an answer is a send back to that slug. With `hive` true the hive tool descriptions start with "You are agent <agent>, one of up to <max_replicas> agents in hive <slug>; the workdir is shared."; with `auto_compact_pct` set the handover description advises handing over once context passes `auto_compact_pct - 15` percent.

```yaml
plugins:
  - name: orchestrator
    plugin: any_mcp.orchestrator:OrchestratorPlugin
    allow: [me, leadership_handover, lock_*, init_scripts_*]
    config:
      base_url: http://orchestrator-agents:8081
      token: ${ORCH_AGENT_TOKEN}
      session: s-1
      agent: agent-0
      slug: my-hive
      max_replicas: 3
      hive: true
```

`${ORCH_AGENT_TOKEN}` is expanded by any-mcp from its environment, so the token is never written to the config file. The token is scoped to one session.

## Examples

Each example lives under `examples/` with its own `any-mcp.yaml`; run one with `any-mcp --config examples/<name>/any-mcp.yaml`.

- `examples/hello`: the smallest plugin, one `@tool` method.
- `examples/single_endpoint`: one HTTP endpoint exposed as one tool.
- `examples/github_issues`: a medium plugin with typed config, credentials and a read-only switch.
- `examples/petstore`: a packaged plugin driven by an OpenAPI document.
- `examples/group`: one server hosting several plugins, each under its own namespace.

The [plugin authoring guide](docs/plugin-authoring.md) walks through all of them.

## Documentation

- [Concepts](docs/concepts.md): plugins, hooks, the call pipeline, reload, tool names, errors and security.
- [Writing a plugin](docs/plugin-authoring.md): from a ten-line plugin to a packaged, OpenAPI-driven one.
- [Testing a plugin](docs/testing-plugins.md): the in-process harness and HTTP mocking.
- [Configuration reference](docs/configuration.md): every config key, flag and environment variable.
- [Transport reference](docs/transports.md): stdio, Streamable HTTP, protocol versions and transport security.

## License

MIT, as declared in `pyproject.toml`.
