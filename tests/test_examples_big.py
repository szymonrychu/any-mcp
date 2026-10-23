"""The packaged OpenAPI example and the group-of-APIs config, run offline."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx
import pytest

from any_mcp.testing import PluginHarness

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
PETSTORE = EXAMPLES / "petstore"
PET = {"id": 1, "name": "Rex", "status": "available", "owner": {"id": 9}, "tags": ["a"]}
GROUP_CONFIG = "examples/group/any-mcp.yaml"
PROJECTED = {"id": 1, "name": "Rex", "status": "available"}


@pytest.fixture
def petstore_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(PETSTORE / "src"))
    for module in [m for m in sys.modules if m.split(".")[0] == "petstore_plugin"]:
        sys.modules.pop(module)


async def test_petstore_example(petstore_path: None) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json=PET)
        if request.url.path == "/v1/pets":
            return httpx.Response(200, json=[PET, {**PET, "id": 2}])
        return httpx.Response(200, json=PET)

    ref = "petstore_plugin:PetstorePlugin"
    async with PluginHarness(ref, name="petstore", transport=httpx.MockTransport(handler)) as h:
        tools = await h.list_tools()
        assert [t["name"] for t in tools] == [
            "petstore_createPet",
            "petstore_getPet",
            "petstore_listPets",
        ]
        listed = await h.call("listPets", {"limit": 2})
        got = await h.call("getPet", {"petId": 1})
        created = await h.call("createPet", {"body": {"name": "Rex"}})
    assert json.loads(listed.text) == [PROJECTED, {**PROJECTED, "id": 2}]
    assert json.loads(got.text) == PROJECTED
    assert json.loads(created.text) == PROJECTED
    assert "owner" not in listed.text
    assert seen[0].url == httpx.URL("https://petstore.example.com/v1/pets?limit=2")
    assert seen[1].url.path == "/v1/pets/1"
    assert json.loads(seen[2].content) == {"name": "Rex"}

    plugin_class = importlib.import_module("petstore_plugin").PetstorePlugin
    assert plugin_class.reload_every == 300
    assert plugin_class.name == "petstore"


def test_petstore_entry_point_declared(petstore_path: None) -> None:
    data = tomllib.loads((PETSTORE / "pyproject.toml").read_text())
    assert "any-mcp" in data["project"]["dependencies"]
    assert data["build-system"]["build-backend"] == "hatchling.build"
    points = data["project"]["entry-points"]["any_mcp.plugins"]
    module, _, attr = points["petstore"].partition(":")
    target = getattr(importlib.import_module(module), attr)
    assert target is importlib.import_module("petstore_plugin").PetstorePlugin
    assert target.__name__ == "PetstorePlugin"


async def test_group_config(monkeypatch: pytest.MonkeyPatch, petstore_path: None) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    for module in ("hello", "github_issues"):
        sys.modules.pop(module, None)
    harness = PluginHarness.from_config(
        EXAMPLES / "group" / "any-mcp.yaml",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
    )
    async with harness as h:
        names = [t["name"] for t in await h.list_tools()]
    assert names == sorted(names)
    assert "hello_greet" in names
    assert {"gh_list_issues", "gh_get_issue", "gh_add_comment"} <= set(names)
    assert {"petstore_listPets", "petstore_getPet", "petstore_createPet"} <= set(names)
    assert "petstore_deletePet" not in names


def test_group_cli_list_tools() -> None:
    env = {**os.environ, "GITHUB_TOKEN": "tok"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "any_mcp",
            "--config",
            "examples/group/any-mcp.yaml",
            "--list-tools",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    names = [t["name"] for t in json.loads(proc.stdout)]
    assert names == sorted(names)
    assert "hello_greet" in names and "gh_list_issues" in names and "petstore_listPets" in names
