"""The guides' code is real: marked blocks equal their files, python blocks compile and run."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUIDES = [ROOT / "docs" / "plugin-authoring.md", ROOT / "docs" / "testing-plugins.md"]
MARKED = re.compile(r"<!-- example: (\S+) -->\n```(\w+)\n(.*?)```", re.DOTALL)
FENCE = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)


def test_example_blocks_match_files() -> None:
    found: set[str] = set()
    for guide in GUIDES:
        for path, _lang, body in MARKED.findall(guide.read_text()):
            assert body == (ROOT / path).read_text(), f"{guide.name}: block differs from {path}"
            found.add(path)
    expected = {
        "examples/hello/hello.py",
        "examples/single_endpoint/zippopotam.py",
        "examples/petstore/src/petstore_plugin/__init__.py",
        "examples/group/any-mcp.yaml",
        "examples/confirm_delete.py",
    }
    assert expected <= found, f"unmarked examples: {sorted(expected - found)}"


def test_python_blocks_compile() -> None:
    count = 0
    for guide in GUIDES:
        for number, block in enumerate(FENCE.findall(guide.read_text()), start=1):
            compile(block, f"{guide.name}[python block {number}]", "exec")
            count += 1
    assert count >= 8


def _section(text: str, heading: str) -> str:
    start = text.index(f"## {heading}\n")
    rest = text[start + 1 :]
    match = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: match.start()] if match else rest


async def test_testing_snippet_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    section = _section((ROOT / "docs" / "testing-plugins.md").read_text(), "The harness")
    blocks = FENCE.findall(section)
    assert blocks, "The harness section has no python block"
    namespace: dict[str, object] = {}
    exec(compile(blocks[0], "testing-plugins.md[The harness]", "exec"), namespace)  # noqa: S102
    tests = [v for k, v in namespace.items() if k.startswith("test_") and callable(v)]
    assert tests, "the snippet defines no test_ function"
    for fn in tests:
        outcome = fn()
        if inspect.isawaitable(outcome):
            await outcome
