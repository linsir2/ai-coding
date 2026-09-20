"""todo_write tool — in-memory structured todo list.

The list persists across tool calls within a session but is not written to disk.
Matches the Java ``TodoWriteTool`` semantics: add / update / list with statuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool


@dataclass
class _TodoItem:
    id: str
    content: str
    status: str = "pending"


class TodoWriteTool(BaseTool):
    """A simple in-memory todo list tool."""

    def __init__(self) -> None:
        self._items: list[_TodoItem] = []
        self._next_id: int = 1

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Manage a todo list. Actions: "
            "add (content) → create a new item; "
            "update (id, status) → change status (pending/in_progress/completed); "
            "list → show all items. "
            "The todo list is in-memory and persists across tool calls."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "update", "list"],
                    "description": "The action to perform.",
                },
                "content": {
                    "type": "string",
                    "description": "Content for 'add' action.",
                },
                "id": {
                    "type": "string",
                    "description": "Item id for 'update' action.",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": "New status for 'update' action.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        action = str(params.get("action", "")).lower()

        if action == "add":
            content = str(params.get("content", "")).strip()
            if not content:
                return ToolResult(success=False, output="error: 'content' is required for 'add'")
            item_id = str(self._next_id)
            self._next_id += 1
            self._items.append(_TodoItem(id=item_id, content=content))
            return ToolResult(
                success=True,
                output=f"added todo #{item_id}: {content}",
            )

        if action == "update":
            item_id = str(params.get("id", ""))
            status = str(params.get("status", "")).lower()
            valid_statuses = {"pending", "in_progress", "completed"}
            if status not in valid_statuses:
                return ToolResult(
                    success=False,
                    output=f"error: status must be one of {sorted(valid_statuses)}",
                )
            for item in self._items:
                if item.id == item_id:
                    item.status = status
                    return ToolResult(
                        success=True,
                        output=f"updated todo #{item_id} to status '{status}'",
                    )
            return ToolResult(success=False, output=f"error: todo #{item_id} not found")

        if action == "list":
            if not self._items:
                return ToolResult(success=True, output="(no todos)")
            lines = []
            for item in self._items:
                lines.append(f"  [{item.status}] #{item.id}  {item.content}")
            return ToolResult(success=True, output="\n".join(lines))

        return ToolResult(
            success=False,
            output=f"error: unknown action '{action}' (valid: add, update, list)",
        )


__all__ = ["TodoWriteTool"]
