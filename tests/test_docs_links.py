"""README and docs: links resolve, the quickstart is real, the install forms are listed."""

from __future__ import annotations

import re
from pathlib import Path

from any_mcp.config import load_config

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} +(.+?)\s*#*\s*$")
FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.DOTALL | re.MULTILINE)


def _slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and (m := HEADING.match(line)):
            anchors.add(_slug(m.group(1)))
    return anchors


def _links(path: Path) -> list[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    return [t for t in LINK.findall(text) if not re.match(r"[a-z][a-z0-9+.-]*:", t)]


def _section(text: str, heading: str) -> str:
    start = text.index(f"## {heading}\n")
    rest = text[start + 1 :]
    match = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: match.start()] if match else rest


def test_relative_links_resolve() -> None:
    checked = 0
    for page in PAGES:
        for target in _links(page):
            file_part, _, anchor = target.partition("#")
            dest = (page.parent / file_part).resolve() if file_part else page
            assert dest.exists(), f"{page.name}: {target} points to a missing file"
            if anchor:
                assert dest.suffix == ".md", f"{page.name}: {target} anchors a non-markdown file"
                assert anchor in _anchors(dest), f"{page.name}: {target} has no such heading"
            checked += 1
    assert checked >= 8


def test_readme_documents_every_page() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for doc in sorted((ROOT / "docs").glob("*.md")):
        assert f"(docs/{doc.name})" in readme, f"README does not link docs/{doc.name}"


def test_readme_quickstart_matches_example(tmp_path: Path) -> None:
    quickstart = _section((ROOT / "README.md").read_text(encoding="utf-8"), "Quickstart")
    blocks = {lang: body for lang, body in FENCE.findall(quickstart)}
    assert blocks["python"] == (ROOT / "examples/hello/hello.py").read_text(encoding="utf-8")
    config = tmp_path / "any-mcp.yaml"
    config.write_text(blocks["yaml"], encoding="utf-8")
    loaded = load_config(config)
    assert [p.plugin for p in loaded.plugins] == ["hello:Hello"]
    assert "any-mcp --config any-mcp.yaml" in quickstart
    assert "claude mcp add" in quickstart


def test_readme_install_forms() -> None:
    install = _section((ROOT / "README.md").read_text(encoding="utf-8"), "Installation")
    forms = [
        "pip install any-mcp",
        "pip install git+https://github.com/szymonrychu/any-mcp",
        'pip install "any-mcp @ git+https://github.com/szymonrychu/any-mcp@v0.5.0"',
        "pip install .",
        "pipx install any-mcp",
        "uv tool install any-mcp",
        "any-mcp --version",
    ]
    commands = {line.split("  #")[0].strip() for line in install.splitlines()}
    for form in forms:
        assert form in commands, form
    marker = "once published to an index; until then use the git forms"
    assert marker in install
    agent = (
        'python -m venv /opt/any-mcp && /opt/any-mcp/bin/pip install "any-mcp @ '
        'git+https://github.com/szymonrychu/any-mcp@v0.5.0"'
    )
    assert agent in install
    assert "/opt/any-mcp/bin/any-mcp" in install
    assert "Agent hook install" in install


def test_concepts_sections() -> None:
    text = (ROOT / "docs/concepts.md").read_text(encoding="utf-8")
    for heading in (
        "Plugins",
        "Hooks",
        "Call pipeline",
        "Reload",
        "Tool names",
        "Errors",
        "Security",
    ):
        assert f"## {heading}\n" in text, heading
    hooks = _section(text, "Hooks")
    for hook in (
        "setup",
        "teardown",
        "load_tools",
        "credentials",
        "before_call",
        "request_input",
        "call",
        "shape_response",
        "on_error",
    ):
        assert f"`{hook}`" in hooks, hook
