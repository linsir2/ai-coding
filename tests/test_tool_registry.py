"""TDD (M2-3): ToolRegistry — register/get/list tools."""

import pytest

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _FakeTool(BaseTool):
    def __init__(self, name: str = "fake") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"A fake tool named {self._name}"

    @property
    def params_json_schema(self):
        return {"type": "object", "properties": {"x": {"type": "string"}}}

    async def execute(self, params):
        return ToolResult(success=True, output="ok")


def test_register_and_get():
    reg = ToolRegistry()
    t = _FakeTool("foo")
    reg.register(t)
    assert reg.get("foo") is t


def test_get_missing_none():
    reg = ToolRegistry()
    assert reg.get("nope") is None


def test_list_names():
    reg = ToolRegistry()
    reg.register(_FakeTool("a"))
    reg.register(_FakeTool("b"))
    assert set(reg.list_names()) == {"a", "b"}


def test_duplicate_name_rejected():
    reg = ToolRegistry()
    reg.register(_FakeTool("same"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_FakeTool("same"))


def test_all_tools_property():
    reg = ToolRegistry()
    reg.register(_FakeTool("a"))
    reg.register(_FakeTool("b"))
    assert len(reg.all_tools) == 2
    assert {t.name for t in reg.all_tools} == {"a", "b"}
