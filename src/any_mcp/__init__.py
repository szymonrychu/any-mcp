from importlib.metadata import version

from any_mcp.context import NoConfig, PluginContext
from any_mcp.errors import Deny, ToolInputError, UpstreamError
from any_mcp.plugin import Plugin, tool
from any_mcp.types import (
    HttpBinding,
    InputRequest,
    InputRequired,
    RawResponse,
    ToolCall,
    ToolResult,
    ToolSpec,
)

__version__ = version("any-mcp")

__all__ = [
    "Deny",
    "HttpBinding",
    "InputRequest",
    "InputRequired",
    "NoConfig",
    "Plugin",
    "PluginContext",
    "RawResponse",
    "ToolCall",
    "ToolInputError",
    "ToolResult",
    "ToolSpec",
    "UpstreamError",
    "__version__",
    "tool",
]
