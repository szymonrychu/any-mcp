"""A small plugin for the stdio CLI tests: logs, fails on demand and records its teardown."""

from __future__ import annotations

import os
from pathlib import Path

from any_mcp import Plugin, tool


class StdioPlugin(Plugin):
    name = "stdio"

    async def setup(self) -> None:
        self.ctx.log.info("stdio plugin set up")

    async def teardown(self) -> None:
        self.ctx.log.warning("stdio plugin tearing down")
        mark = os.environ.get("ANY_MCP_TEST_MARK")
        if mark:
            Path(mark).write_text("teardown\n", encoding="utf-8")

    @tool
    async def echo(self, text: str) -> str:
        """Echo the text back."""
        self.ctx.log.info("echo called with %s", text)
        return text

    @tool
    async def boom(self) -> str:
        """Always fail."""
        self.ctx.log.error("boom is about to fail\nwith a second line")
        raise RuntimeError("boom")
