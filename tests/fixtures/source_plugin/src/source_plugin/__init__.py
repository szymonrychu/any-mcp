"""Fixture plugin package for the plugin source tests: one entry point, one tool."""

from __future__ import annotations

from any_mcp import Plugin, tool


class SourcePlugin(Plugin):
    name = "source_plugin"

    @tool
    async def ping(self) -> str:
        """Answer pong."""
        return "pong"
