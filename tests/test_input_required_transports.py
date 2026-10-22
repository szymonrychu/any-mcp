"""InputRequiredResult round trips through the real ``any-mcp`` over stdio and HTTP."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from mcp_types import InputRequiredResult

ROOT = Path(__file__).resolve().parent.parent
TOOL = "records_delete_record"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "any-mcp.yaml"
    path.write_text(
        f"python_path: ['{ROOT / 'examples'}']\n"
        "plugins:\n  - name: records\n    plugin: confirm_delete:ConfirmDelete\n",
        encoding="utf-8",
    )
    return path


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "ANY_MCP_CONFIG"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_url(config: Path) -> Iterator[str]:
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "any_mcp",
            "--config",
            str(config),
            "--transport",
            "http",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_env(),
        cwd=ROOT,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError("any-mcp http exited early")
            try:
                httpx.get(f"http://127.0.0.1:{port}/", timeout=1)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@asynccontextmanager
async def _client(kind: str, config: Path, url: str, mode: str) -> AsyncIterator[Client[Any]]:
    if kind == "stdio":
        transport: Any = StdioTransport(
            sys.executable, ["-m", "any_mcp", "--config", str(config)], env=_env(), cwd=str(ROOT)
        )
    else:
        transport = StreamableHttpTransport(url)
    async with Client(transport, mode=mode) as client:
        yield client


async def _round_trip(client: Client[Any], confirm: bool) -> list[str]:
    first = await client.session.call_tool(TOOL, {"record_id": "r1"}, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    assert first.result_type == "input_required"
    assert first.input_requests is not None and list(first.input_requests) == ["confirm"]
    request = first.input_requests["confirm"]
    assert request.method == "elicitation/create"
    assert request.params.message == "Delete record r1?"
    assert first.request_state is not None
    done = await client.session.call_tool(
        TOOL,
        {"record_id": "r1"},
        input_responses={"confirm": {"action": "accept", "content": {"confirm": confirm}}},
        request_state=first.request_state,
    )
    assert not done.is_error
    assert done.structured_content == {"deleted": confirm, "record_id": "r1"}
    listed = await client.session.call_tool("records_list_records", {})
    return list(listed.structured_content["records"])


async def test_stdio_round_trip(config: Path) -> None:
    async with _client("stdio", config, "", "auto") as client:
        assert client.protocol_version == "2026-07-28"
        assert await _round_trip(client, True) == ["r2"]


async def test_http_round_trip(config: Path, http_url: str) -> None:
    async with _client("http", config, http_url, "auto") as client:
        assert client.protocol_version == "2026-07-28"
        assert await _round_trip(client, True) == ["r2"]


async def test_declined_answer(config: Path, http_url: str) -> None:
    async with _client("stdio", config, "", "auto") as client:
        assert await _round_trip(client, False) == ["r1", "r2"]
    async with _client("http", config, http_url, "auto") as client:
        assert await _round_trip(client, False) == ["r1", "r2"]


async def test_legacy_client_gets_error(config: Path, http_url: str) -> None:
    for kind in ("stdio", "http"):
        async with _client(kind, config, http_url, "legacy") as client:
            assert client.protocol_version != "2026-07-28"
            result = await client.session.call_tool(TOOL, {"record_id": "r1"})
            assert result.is_error is True
            text = result.content[0].text
            assert "2026-07-28" in text and "client" in text
            listed = await client.session.call_tool("records_list_records", {})
            assert list(listed.structured_content["records"]) == ["r1", "r2"]
            caps = client.server_capabilities
            assert caps is not None and caps.tools is not None
            dumped = caps.model_dump(exclude_none=True, exclude_unset=True)
            assert set(dumped) == {"tools"}


async def test_modern_capabilities_tools_only(config: Path) -> None:
    async with _client("stdio", config, "", "auto") as client:
        caps = client.server_capabilities
        assert caps is not None
        assert set(caps.model_dump(exclude_none=True, exclude_unset=True)) == {"tools"}


async def test_retry_carries_request_state(config: Path) -> None:
    """The adapter hands the retry's requestState to the plugin (the example needs it)."""
    async with _client("stdio", config, "", "auto") as client:
        first = await client.session.call_tool(TOOL, {"record_id": "r1"}, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        assert first.request_state is not None
        answer = {"confirm": {"action": "accept", "content": {"confirm": True}}}
        right = await client.session.call_tool(
            TOOL, {"record_id": "r1"}, input_responses=answer, request_state=first.request_state
        )
        assert right.structured_content == {"deleted": True, "record_id": "r1"}
