"""The configuration and transport references name every config key and CLI flag."""

from __future__ import annotations

import re
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from any_mcp import _fastmcp_adapter
from any_mcp.cli import build_parser
from any_mcp.config import AnyMcpConfig

DOCS = Path(__file__).resolve().parent.parent / "docs"
CONFIGURATION = (DOCS / "configuration.md").read_text(encoding="utf-8")
TRANSPORTS = (DOCS / "transports.md").read_text(encoding="utf-8")


def _models(annotation: Any) -> list[tuple[type[BaseModel], bool]]:
    """The BaseModel types inside an annotation, flagged when they sit in a list."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return [m for arg in get_args(annotation) for m in _models(arg)]
    if origin is list:
        return [(m, True) for arg in get_args(annotation) for m, _ in _models(arg)]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [(annotation, False)]
    return []


def config_keys(model: type[BaseModel], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for name, field in model.model_fields.items():
        if field.exclude:
            continue
        path = f"{prefix}{name}"
        keys.append(path)
        for child, in_list in _models(field.annotation):
            keys.extend(config_keys(child, f"{path}[]." if in_list else f"{path}."))
    return keys


def sections(text: str) -> set[str]:
    return {h.strip("`") for h in re.findall(r"^#{1,6} (.+?)\s*$", text, re.MULTILINE)}


def test_every_config_key_documented():
    keys = config_keys(AnyMcpConfig)
    assert "server.http.bearer_token" in keys and "plugins[].reload_every" in keys
    missing = [k for k in keys if f"`{k}`" not in CONFIGURATION]
    assert not missing, f"undocumented config keys: {missing}"


def test_every_cli_flag_documented():
    flags = [
        opt
        for action in build_parser()._actions
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    ]
    assert "--list-tools" in flags
    missing = [f for f in flags if f"`{f}" not in CONFIGURATION + TRANSPORTS]
    assert not missing, f"undocumented CLI flags: {missing}"


def test_required_sections_present():
    assert {
        "File format",
        "Environment variables",
        "server",
        "tools",
        "plugins",
        "Exit codes",
    } <= sections(CONFIGURATION)
    assert {
        "stdio",
        "Streamable HTTP",
        "Protocol versions",
        "Tool list changes",
        "Security",
        "Registering with Claude Code",
    } <= sections(TRANSPORTS)


def test_tool_list_changes_carries_adapter_probe_notes():
    """The probe results are copied from the adapter docstring, so they cannot drift apart."""
    doc = " ".join(TRANSPORTS.split())
    for needle in (
        "subscriptions/listen",
        "InMemorySubscriptionBus",
        "ToolsListChanged",
        "notifications/tools/list_changed",
        "send_tool_list_changed",
        "FunctionTool",
        "isError",
        "ListenHandler",
    ):
        assert needle in doc, needle
        assert needle in " ".join((_fastmcp_adapter.__doc__ or "").split()), needle
