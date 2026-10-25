# Changelog

## v0.3.0

Breaking: the orchestrator plugin follows the agent-orchestrator rename of clusters to sessions.
Requires agents-orchestrator with the session agent API.

- Config key `cluster` is now `session`.
- The request header `X-Orch-Cluster` is now `X-Orch-Session`.
- Error codes `static_cluster` and `next_cluster` are now `static_session` and `next_session`.

## v0.2.0

Adds the built-in `OrchestratorPlugin` (plugin name `orchestrator`, `any_mcp.orchestrator`): agents
call the agent-orchestrator agent API (`/agent/v1`) as MCP tools.

- Nine tools: `orchestrator_me`, `orchestrator_leadership_handover`, `orchestrator_lock_claim`,
  `orchestrator_lock_release`, `orchestrator_lock_list`, `orchestrator_lock_reassign`,
  `orchestrator_scale` (leader only, range 1..max_replicas), `orchestrator_init_scripts_get` and
  `orchestrator_init_scripts_edit`.
- Config (all required unless noted): `base_url`, `token`, `cluster`, `agent`, `slug`;
  optional `max_replicas` (default 1), `static`, `hive` (hive wording in tool descriptions) and
  `auto_compact_pct` (adds handover-instead-of-compacting advice to the handover tool).
- Credentials: the per-cluster token is sent as `Authorization: Bearer` with an `X-Orch-Cluster`
  header on every call, and is redacted from error output. Orchestrator error codes surface as
  tool errors with the HTTP status.

## v0.1.0

First release: any-mcp exposes any API, or a group of APIs, as MCP tools through plugins. The
server speaks the MCP tools capability.

- Plugin API: a plugin is one class subclassing `Plugin`; only `load_tools` is required (or `@tool`
  methods), every other hook has a default. The core owns argument validation, error mapping, tool
  naming, reload and response size budgets, so a plugin holds only the business logic of its API.
- Optional input-required hook: a tool can pause and ask the user for confirmation or missing input
  before it runs, and the answer flows back into the call.
- Discovery: plugins are found through installed entry points, `module:Class` references, or a
  config file, and one server can load many plugins, each in its own namespace.
- Transports: stdio (default) and Streamable HTTP, both on FastMCP 4.x behind one adapter, with
  graceful shutdown on SIGTERM and SIGINT.
- OpenAPI plugin: turns an OpenAPI document into MCP tools without writing code.
- Test harness: `any_mcp.testing` runs a plugin in process so plugin authors can test tools without
  starting a server.
- Examples: hello, single endpoint, group of APIs, petstore (OpenAPI), GitHub issues and a
  confirm-before-delete tool under `examples/`.
- Docs: README, concepts guide, plugin authoring, configuration, transports and testing guides.
- Packaging: a standard pip package with the `any-mcp` console script (`any-mcp --version`).
  Install from the git tag, from a checkout, with pipx or with `uv tool install`.

### Install (pinned release, as the agent onStart script runs it)

```bash
python -m venv /opt/any-mcp && /opt/any-mcp/bin/pip install "any-mcp @ git+https://github.com/szymonrychu/any-mcp@v0.1.0"
```

The console script is `/opt/any-mcp/bin/any-mcp`. The config file may be JSON (JSON is valid
YAML), so the hook can write it with the standard library only.
