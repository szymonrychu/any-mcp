"""Logging setup: every record goes to stderr, because stdout belongs to MCP on stdio."""

from __future__ import annotations

import logging
import sys

LEVELS = ("debug", "info", "warning", "error")
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _OneLine(logging.Formatter):
    """A record never spans lines, so a log reader can treat each line as one record."""

    def format(self, record: logging.LogRecord) -> str:
        return " | ".join(super().format(record).splitlines())


def configure(level: str = "info") -> None:
    """Send the root logger to stderr at ``level``, replacing earlier any-mcp handlers."""
    name = level.lower()
    if name not in LEVELS:
        raise ValueError(f"unknown log level {level!r}; use one of {', '.join(LEVELS)}")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_OneLine(_FORMAT))
    handler.set_name("any-mcp-stderr")
    root = logging.getLogger()
    for old in [h for h in root.handlers if h.get_name() == "any-mcp-stderr"]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(name.upper())
