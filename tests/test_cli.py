"""The ``any-mcp`` command over stdio, run as a real subprocess."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import any_mcp
from any_mcp.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "tests.fixtures.stdio_plugin:StdioPlugin"
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "ANY_MCP_CONFIG"}
    env.update(extra)
    return env


def _run(
    *args: str, env: dict[str, str] | None = None, stdin: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "any_mcp", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=env or _env(),
        cwd=ROOT,
    )


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "any-mcp.yaml"
    path.write_text(
        f'python_path: ["{ROOT}"]\nplugins:\n  - name: p\n    plugin: "{PLUGIN}"\n',
        encoding="utf-8",
    )
    return path


class Stdio:
    """A running ``any-mcp --config`` process spoken to in newline-delimited JSON-RPC."""

    def __init__(self, config: Path, mark: Path | None = None) -> None:
        env = _env(**({"ANY_MCP_TEST_MARK": str(mark)} if mark else {}))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "any_mcp", "--config", str(config)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=ROOT,
        )
        self.lines: list[str] = []
        self._id = 0

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.lines.append(line)
        reply = json.loads(line)
        assert reply["id"] == self._id
        return reply

    def wait(self, timeout: float = 10) -> int:
        return self.proc.wait(timeout)

    def close(self) -> tuple[int, str]:
        self.proc.stdin.close()
        code = self.wait(5)
        self.lines.extend(self.proc.stdout.read().splitlines(keepends=True))
        return code, self.proc.stderr.read()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture
def stdio(config: Path, tmp_path: Path):
    session = Stdio(config, tmp_path / "mark")
    yield session
    session.kill()


def _legacy(session: Stdio) -> None:
    reply = session.request(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert reply["result"]["protocolVersion"] == "2025-11-25"
    session.notify("notifications/initialized")


def test_help_and_version() -> None:
    helped = _run("--help")
    assert helped.returncode == 0
    for flag in ("--config", "--plugin", "--transport", "--log-level", "--list-tools", "--version"):
        assert flag in helped.stdout
    versioned = _run("--version")
    assert versioned.returncode == 0
    assert versioned.stdout.strip() == f"any-mcp {any_mcp.__version__}"
    parsed = build_parser().parse_args(["--plugin", "a:B", "--plugin", "c:D"])
    assert parsed.plugin == ["a:B", "c:D"]


def test_no_config_exits_78() -> None:
    result = _run()
    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr == "any-mcp: no config: pass --config, --plugin or set ANY_MCP_CONFIG\n"


def test_config_errors_exit_78(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("plugins:\n  - name: p\n    bogus: 1\n", encoding="utf-8")
    result = _run("--config", str(bad))
    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert "plugins.0.plugin" in result.stderr or "bogus" in result.stderr

    missing = _run("--config", str(tmp_path / "nope.yaml"))
    assert missing.returncode == 78 and missing.stdout == ""
    assert "nope.yaml" in missing.stderr and missing.stderr.count("\n") == 1

    unloadable = _run("--plugin", "no_such_module_xyz:Nope")
    assert unloadable.returncode == 78 and unloadable.stdout == ""
    assert "no_such_module_xyz:Nope" in unloadable.stderr
    assert unloadable.stderr.count("\n") == 1

    class_missing = _run("--plugin", "any_mcp.config:Nope")
    assert class_missing.returncode == 78 and class_missing.stdout == ""
    assert "any_mcp.config:Nope" in class_missing.stderr


def test_list_tools_flag(config: Path, tmp_path: Path) -> None:
    mark = tmp_path / "mark"
    result = _run("--config", str(config), "--list-tools", env=_env(ANY_MCP_TEST_MARK=str(mark)))
    assert result.returncode == 0, result.stderr
    tools = json.loads(result.stdout)
    assert [t["name"] for t in tools] == ["p_boom", "p_echo"]
    echo = tools[1]
    assert echo["description"] == "Echo the text back."
    assert echo["inputSchema"]["required"] == ["text"]
    assert mark.read_text() == "teardown\n"

    extra = _run("--config", str(config), "--plugin", PLUGIN, "--list-tools")
    assert extra.returncode == 0, extra.stderr
    assert [t["name"] for t in json.loads(extra.stdout)] == [
        "p_boom",
        "p_echo",
        "stdioplugin_boom",
        "stdioplugin_echo",
    ]


def test_plugin_flag_without_config() -> None:
    result = _run("--plugin", PLUGIN, "--list-tools", env=_env(PYTHONPATH=str(ROOT)))
    assert result.returncode == 0, result.stderr
    assert [t["name"] for t in json.loads(result.stdout)] == [
        "stdioplugin_boom",
        "stdioplugin_echo",
    ]


def test_config_from_env(config: Path) -> None:
    result = _run("--list-tools", env=_env(ANY_MCP_CONFIG=str(config)))
    assert result.returncode == 0, result.stderr
    assert [t["name"] for t in json.loads(result.stdout)] == ["p_boom", "p_echo"]


def test_stdio_legacy_handshake(stdio: Stdio) -> None:
    _legacy(stdio)
    listed = stdio.request("tools/list")["result"]
    assert [t["name"] for t in listed["tools"]] == ["p_boom", "p_echo"]
    called = stdio.request("tools/call", {"name": "p_echo", "arguments": {"text": "hi"}})["result"]
    assert called["isError"] is False
    assert called["content"][0]["text"] == "hi"
    code, _ = stdio.close()
    assert code == 0


def test_stdio_modern_discover(stdio: Stdio) -> None:
    found = stdio.request("server/discover", {"_meta": META})["result"]
    assert "2026-07-28" in found["supportedVersions"]
    assert "tools" in found["capabilities"]
    listed = stdio.request("tools/list", {"_meta": META})["result"]
    assert listed["ttlMs"] == 30000
    assert [t["name"] for t in listed["tools"]] == ["p_boom", "p_echo"]
    called = stdio.request(
        "tools/call", {"name": "p_echo", "arguments": {"text": "hi"}, "_meta": META}
    )["result"]
    assert called["isError"] is False
    assert called["content"][0]["text"] == "hi"
    code, _ = stdio.close()
    assert code == 0


def test_stdout_only_mcp_messages(stdio: Stdio) -> None:
    _legacy(stdio)
    failed = stdio.request("tools/call", {"name": "p_boom", "arguments": {}})["result"]
    assert failed["isError"] is True
    stdio.request("tools/call", {"name": "p_echo", "arguments": {"text": "a\nb"}})
    code, stderr = stdio.close()
    assert code == 0
    assert len(stdio.lines) == 3
    for line in stdio.lines:
        assert line.endswith("\n") and line.count("\n") == 1
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
    assert "stdio plugin set up" in stderr
    assert "boom is about to fail | with a second line" in stderr


def test_exit_on_stdin_eof(stdio: Stdio, tmp_path: Path) -> None:
    _legacy(stdio)
    start = time.monotonic()
    code, _ = stdio.close()
    assert code == 0
    assert time.monotonic() - start < 5
    assert (tmp_path / "mark").read_text() == "teardown\n"


def test_sigterm_teardown(stdio: Stdio, tmp_path: Path) -> None:
    _legacy(stdio)
    stdio.proc.send_signal(signal.SIGTERM)
    assert stdio.wait(5) == 0
    assert (tmp_path / "mark").read_text() == "teardown\n"
    assert stdio.proc.stdout.read() == ""
