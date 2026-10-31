"""A model writes tool names as words; the registry spells them camelCase.

Measured trace 2026-10-31 (t1): five ``unknown_tool`` failures because the
model called ``subagent`` for the registry's ``subAgent``.  Nothing in the
registry differs only by case, so an exact hit followed by a case-insensitive
fallback cannot shadow a real tool.
"""

from types import SimpleNamespace

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Tool(BaseTool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        return ToolResult(success=True, output="ok")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_Tool("subAgent"))
    registry.register(_Tool("tool_search"))
    return registry


def test_a_lowercase_call_reaches_the_camel_case_tool():
    tool = _registry().get("subagent")

    assert tool is not None and tool.name == "subAgent"


def test_an_exact_hit_still_wins():
    assert _registry().get("tool_search").name == "tool_search"  # type: ignore[union-attr]


def test_an_unknown_name_stays_unknown():
    assert _registry().get("not_a_tool") is None


class _Gate:
    """Only the one method ``_prepare`` asks for."""

    def check(self, name, params, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(decision="allow", specific=False)


def test_the_alias_normalizes_the_call_before_guards_and_ledgers_see_it():
    """A guard matches exact names (``bash``, ``present_plan``…).  Resolving
    ``Bash`` to the tool but passing the model's spelling to the guard would run
    the red-line checks past a call they must see."""
    dispatcher = ToolDispatcher(_registry(), _Gate())

    prepared = dispatcher._prepare(
        ToolCallRef(id="c1", name="subagent", arguments="{}"), ctx=ToolContext()
    )

    assert prepared.tool is not None and prepared.tool.name == "subAgent"
    assert prepared.call.name == "subAgent"
