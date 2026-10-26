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

## Plugin sources

A plugin does not have to be importable already: a `source:` on a plugin entry names a Python package that any-mcp installs and loads. The notation is a subset of Terraform's module sources:

- local directory: `./plugins/mine`, `../shared/plugin`, `/abs/path/plugin`
- git: `git::https://example.com/acme/mcp-plugins.git//topic/tools?ref=v1.2.0`
- GitHub shorthand: `github.com/owner/repo//subdir?ref=v1.2.0`

`//subdir` selects a directory inside the repository and `?ref=` a branch, tag or commit.

```yaml
plugins:
  - name: tools
    source: github.com/szymonrychu/any-mcp-tatara-plugins//topic/tools?ref=main
    config:
      base_url: https://api.example.com
```

Install ahead of the first start, so a slow install fails visibly:

```bash
any-mcp plugins install --config any-mcp.yaml
```

The platform plugins live in [any-mcp-tatara-plugins](https://github.com/szymonrychu/any-mcp-tatara-plugins). See [Configuration](docs/configuration.md#plugin-sources) for the full reference.

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
