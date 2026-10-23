# Changelog

## v0.1.0

First release: any-mcp exposes any API, or a group of APIs, as MCP tools through plugins. It is
tools only.

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
  Install from git, from a checkout, with pipx or with `uv tool install`; there is no PyPI
  publishing.

### Install (pinned release, as the agent onStart script runs it)

```bash
python -m venv /opt/any-mcp && /opt/any-mcp/bin/pip install "any-mcp @ git+https://github.com/szymonrychu/any-mcp@v0.1.0"
```

The console script is `/opt/any-mcp/bin/any-mcp`. The config file may be JSON (JSON is valid
YAML), so the hook can write it with the standard library only.
