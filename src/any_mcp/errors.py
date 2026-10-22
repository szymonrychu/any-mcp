"""Error types shared by the core and plugins, and the mapping from errors to tool results."""

from __future__ import annotations

from any_mcp.types import RawResponse, ToolResult

_BODY_LIMIT = 500


class AnyMcpError(Exception):
    """Base class of every any-mcp error."""


class Deny(AnyMcpError):
    """Raised by a plugin hook to refuse a call; the message is shown to the client."""


class ToolInputError(AnyMcpError):
    """Arguments failed validation. ``errors`` is a list of (JSON path, message)."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = list(errors)
        super().__init__(_describe_input_errors(self.errors))


class UpstreamError(AnyMcpError):
    """The upstream API answered with an error status."""

    def __init__(self, response: RawResponse) -> None:
        self.response = response
        super().__init__(f"upstream returned HTTP {response.status}")


class ConfigError(AnyMcpError):
    """Invalid configuration or plugin reference."""


class CallTimeout(TimeoutError, AnyMcpError):
    """A tool call ran longer than its plugin's timeout."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(f"timed out after {seconds:g}s")


class UnknownTool(AnyMcpError):
    """No tool is registered under the requested name."""


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _describe_input_errors(errors: list[tuple[str, str]]) -> str:
    return "; ".join(f"{path}: {message}" for path, message in errors)


def error_result(exc: BaseException, *, plugin: str, expose_details: bool = False) -> ToolResult:
    """Map an exception to an error ToolResult. No header value ever appears in it."""
    if isinstance(exc, Deny):
        kind, message = "denied", _one_line(str(exc)) or "denied"
        return _result(kind, f"denied: {message}", message)
    if isinstance(exc, ToolInputError):
        message = _one_line(_describe_input_errors(exc.errors)) or "invalid arguments"
        return _result("invalid_arguments", f"invalid arguments: {message}", message)
    if isinstance(exc, UpstreamError):
        status = exc.response.status
        body = _one_line(exc.response.text()[:_BODY_LIMIT])
        message = f"upstream returned HTTP {status}" + (f": {body}" if body else "")
        return _result("upstream", message, message, status=status)
    if isinstance(exc, TimeoutError):
        after = f" after {exc.seconds:g}s" if isinstance(exc, CallTimeout) else ""
        return _result("timeout", f"{plugin} timed out{after}", f"{plugin} timed out{after}")
    message = f"{plugin} failed: {type(exc).__name__}"
    if expose_details and str(exc):
        message += f": {_one_line(str(exc))}"
    return _result("internal", message, message)


def _result(kind: str, text: str, message: str, *, status: int | None = None) -> ToolResult:
    error: dict[str, object] = {"kind": kind, "message": message}
    if status is not None:
        error["status"] = status
    return ToolResult(text=text, structured={"error": error}, is_error=True)
