"""TDD (M2-1): BaseTool abstract contract."""

import pytest

from ai_coding.tools.base import BaseTool


def test_base_tool_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BaseTool()


def test_subclass_must_implement_execute():
    class BadTool(BaseTool):
        @property
        def name(self):
            return "bad"

        @property
        def description(self):
            return "nope"

        @property
        def params_json_schema(self):
            return {"type": "object", "properties": {}}

    with pytest.raises(TypeError):
        BadTool()


def test_good_subclass_can_be_instantiated():
    class GoodTool(BaseTool):
        @property
        def name(self):
            return "good"

        @property
        def description(self):
            return "ok"

        @property
        def params_json_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, params):
            from ai_coding.domain.message import ToolResult
            return ToolResult(success=True, output="done")

    t = GoodTool()
    assert t.name == "good"
    assert t.description == "ok"
