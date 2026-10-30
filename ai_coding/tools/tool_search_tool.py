"""``tool_search``: the model asks for a deferred tool by what it does.

The tool surface is paid on every step.  Twenty-six tools cost ~23.5k
characters in the request, and the expensive half (memory, jobs, parallel
research, skills) is used once in a while.  Those are deferred: the model sees
their names in the one line this tool returns, and the *next* request carries
their schemas.

Loading is a surface change, not a permission: the tool still has to pass the
posture and the rule checks when the next request is built, and the call itself
still goes through the gate.  A denied tool that is "loaded" simply never
appears.  What may be *offered* is the same question asked one step earlier:
``ToolContext.searchable_names`` is the deferred part of this request's own
surface, so a read-only request cannot load a mutating tool either.

Search is deliberately dumb (name/description substring, most specific first):
it runs in-process, costs no model call, and a deferred set this small does not
need relevance ranking.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx
from ai_coding.tools.registry import ToolRegistry

__all__ = ["ToolSearchTool"]

#: How many matches one search may load.  Enough to cover a coherent family
#: (the three job tools), small enough that one query cannot pull the whole
#: deferred catalogue into the request.
MAX_MATCHES = 5


class ToolSearchTool(BaseTool):
    """Find and load a deferred tool by name or purpose."""

    parallel_safe = True
    mutates_world = False

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "tool_search"

    @property
    def description(self) -> str:
        return (
            "Find a tool that is not in this request's catalogue yet and load it. "
            "Call this when you need a capability you do not see (memory, jobs, "
            "parallel research, skills, web). The matched tools are available on "
            "your next step; their full signatures arrive with it."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you need, or part of the tool's name.",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        context = resolve_ctx(ctx)
        query = str((params or {}).get("query") or "").strip()
        if not query:
            return ToolResult(success=False, output="query is empty")

        loaded = set(getattr(context, "loaded_tools", ()) or ())
        searchable = set(getattr(context, "searchable_names", ()) or ())
        matches = self._match(query, loaded, searchable)
        if not matches:
            available = self._deferred_names(loaded, searchable)
            return ToolResult(
                success=True,
                output=(
                    f"no deferred tool matched '{query}'. "
                    f"Still available to load: {', '.join(available) or '(none)'}"
                ),
            )

        names = tuple(tool.name for tool in matches)
        if context.note_tool_loaded is not None:
            context.note_tool_loaded(names)
        lines = [f"已加载 {len(names)} 个工具（下一步可用）："]
        lines.extend(f"- {tool.name}: {_one_line(tool.description)}" for tool in matches)
        return ToolResult(success=True, output="\n".join(lines))

    def _match(
        self, query: str, loaded: set[str], searchable: set[str]
    ) -> list[BaseTool]:
        words = [word for word in query.lower().split() if word]
        found: list[tuple[int, BaseTool]] = []
        for tool in self._registry.all_tools:
            # "May this be loaded" is the request's own surface question, not a
            # registry question: a read-only request cannot load a mutating
            # tool, and a denied one is never on offer.
            if not tool.defer or tool.name in loaded or tool.name not in searchable:
                continue
            haystack = f"{tool.name} {tool.description}".lower()
            if not any(word in haystack for word in words):
                continue
            # Name hits rank above description hits; then registry order.
            score = 0 if query.lower() in tool.name.lower() else 1
            found.append((score, tool))
        found.sort(key=lambda row: (row[0], row[1].name))
        return [tool for _, tool in found[:MAX_MATCHES]]

    def _deferred_names(self, loaded: set[str], searchable: set[str]) -> list[str]:
        return [
            tool.name
            for tool in self._registry.all_tools
            if tool.defer and tool.name not in loaded and tool.name in searchable
        ]


def _one_line(text: str, limit: int = 90) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"
