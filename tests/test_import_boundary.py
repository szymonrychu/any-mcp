"""Only the FastMCP adapter module may import ``fastmcp`` or the ``mcp`` SDK (design D1)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = Path("src/any_mcp/_fastmcp_adapter.py")
SCANNED = ("src/any_mcp", "examples")
# ``mcp_types`` ships with the ``mcp`` SDK; it is part of the same boundary.
FORBIDDEN = frozenset({"fastmcp", "mcp", "mcp_types"})
_DYNAMIC = frozenset({"import_module", "__import__"})


def _top(module: str | None) -> str:
    return (module or "").split(".", 1)[0]


def _imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Every (line, module) a file imports, including ``importlib.import_module("x")``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            first = node.args[0]
            if (
                name in _DYNAMIC
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                found.append((node.lineno, first.value))
    return found


def _python_files() -> list[Path]:
    files: list[Path] = []
    for folder in SCANNED:
        base = ROOT / folder
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return files


def test_only_adapter_imports_fastmcp() -> None:
    files = _python_files()
    rels = {path.relative_to(ROOT) for path in files}
    assert ADAPTER in rels, "the adapter module must exist and be scanned"

    violations: list[str] = []
    adapter_imports: set[str] = set()
    for path in files:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for line, module in _imports(tree):
            if _top(module) not in FORBIDDEN:
                continue
            if rel == ADAPTER:
                adapter_imports.add(_top(module))
            else:
                violations.append(f"{rel}:{line} imports {module}")

    assert not violations, "only the adapter may import fastmcp/mcp:\n" + "\n".join(violations)
    # The scan must be able to see the imports it polices, or it proves nothing.
    assert {"fastmcp", "mcp"} <= adapter_imports


def test_boundary_scan_sees_every_import_form() -> None:
    source = (
        "import fastmcp\n"
        "import mcp.server as s\n"
        "from mcp_types import Tool\n"
        "from fastmcp.tools import base\n"
        "from . import fastmcp\n"
        "import importlib\n"
        "importlib.import_module('fastmcp.server')\n"
        "__import__('mcp')\n"
        "import mcpx\n"
    )
    modules = [m for _, m in _imports(ast.parse(source))]
    flagged = [m for m in modules if _top(m) in FORBIDDEN]
    assert flagged == [
        "fastmcp",
        "mcp.server",
        "mcp_types",
        "fastmcp.tools",
        "fastmcp.server",
        "mcp",
    ]
