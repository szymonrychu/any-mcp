"""A plugin that asks the client to confirm before it deletes (MCP 2026-07-28 input required).

Run it with a config next to this file:

    python_path: ["."]
    plugins:
      - name: records
        plugin: confirm_delete:ConfirmDelete

The first ``records_delete_record`` call returns an input-required result asking
``{"confirm": boolean}``. The client answers and retries the same call with
``inputResponses``; only a yes deletes. It imports only ``any_mcp``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from any_mcp import InputRequest, InputRequired, Plugin, ToolCall, ToolSpec

ID_SCHEMA = {
    "type": "object",
    "properties": {"record_id": {"type": "string"}},
    "required": ["record_id"],
    "additionalProperties": False,
}
CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {"confirm": {"type": "boolean", "title": "Delete it?"}},
    "required": ["confirm"],
}


def _confirmed(call: ToolCall) -> bool:
    """Whether the client accepted the form and answered ``confirm: true``."""
    answer = call.input_responses.get("confirm")
    if not isinstance(answer, dict) or answer.get("action") != "accept":
        return False
    return (answer.get("content") or {}).get("confirm") is True


class ConfirmDelete(Plugin):
    name = "records"

    def __init__(self) -> None:
        self.records: dict[str, str] = {"r1": "first", "r2": "second"}

    async def load_tools(self) -> Sequence[ToolSpec]:
        async def delete_record(call: ToolCall) -> dict[str, Any]:
            record_id = call.arguments["record_id"]
            if not _confirmed(call):
                return {"deleted": False, "record_id": record_id}
            removed = self.records.pop(record_id, None) is not None
            return {"deleted": removed, "record_id": record_id}

        async def list_records(call: ToolCall) -> dict[str, Any]:
            return {"records": sorted(self.records)}

        return [
            ToolSpec(
                name="delete_record",
                description="Delete a record after the client confirms.",
                input_schema=ID_SCHEMA,
                handler=delete_record,
            ),
            ToolSpec(
                name="list_records",
                description="List record ids.",
                input_schema={"type": "object", "additionalProperties": False},
                handler=list_records,
            ),
        ]

    async def request_input(self, call: ToolCall) -> InputRequired | None:
        if call.tool != "delete_record" or "confirm" in call.input_responses:
            return None
        record_id = call.arguments["record_id"]
        question = InputRequest("elicitation", f"Delete record {record_id}?", CONFIRM_SCHEMA)
        return InputRequired({"confirm": question}, state=record_id)
