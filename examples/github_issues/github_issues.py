"""A multi-tool plugin with auth: list and read GitHub issues, optionally comment."""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from any_mcp import Deny, Plugin, RawResponse, ToolCall, ToolResult, tool


class Config(BaseModel):
    token: str
    base_url: str = "https://api.github.com"  # also becomes ctx.http's base URL
    read_only: bool = True


class GithubIssues(Plugin):
    name = "github"
    Config = Config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.ctx.config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def credentials(self, call: ToolCall) -> Mapping[str, str]:
        return self._headers()

    async def before_call(self, call: ToolCall) -> ToolCall:
        if call.tool == "add_comment" and self.ctx.config.read_only:
            raise Deny("add_comment is disabled: the plugin is configured read_only")
        return call

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        # Tool methods call ctx.http directly, so they add the auth headers themselves.
        response = await self.ctx.http.request(method, path, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response.json()

    @tool
    async def list_issues(
        self, repo: str, state: str = "open", limit: int = 10
    ) -> list[dict[str, Any]]:
        """List issues of a repository ("owner/name"), filtered by state."""
        params = {"state": state, "per_page": limit}
        return await self._request("GET", f"/repos/{repo}/issues", params=params)

    @tool
    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        """Fetch one issue by number."""
        return await self._request("GET", f"/repos/{repo}/issues/{number}")

    @tool
    async def add_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        """Comment on an issue. Refused while the plugin is read_only."""
        path = f"/repos/{repo}/issues/{number}/comments"
        return await self._request("POST", path, json={"body": body})

    async def shape_response(self, call: ToolCall, raw: RawResponse) -> ToolResult:
        value = raw.json()
        if isinstance(value, list):
            value = [_project(item) for item in value]
        elif call.tool == "get_issue":
            value = _project(value)
        return await super().shape_response(call, RawResponse.from_value(value))


def _project(issue: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "state": issue["state"],
        "url": issue["html_url"],
    }
