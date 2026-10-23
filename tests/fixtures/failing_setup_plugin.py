"""A plugin whose setup always fails, for the cleanup-on-failure tests."""

from __future__ import annotations

from any_mcp import Plugin, tool


class FailingSetup(Plugin):
    name = "failing"

    async def setup(self) -> None:
        raise RuntimeError("setup failed")

    @tool
    async def noop(self) -> str:
        """Nothing."""
        return ""
