"""TDD (R1a-1): BaseTool -> OpenAI function schema."""

import json

from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.schema import tool_schema, tool_schemas, tool_schemas_from_tools
from ai_coding.tools.todo_write_tool import TodoWriteTool


def test_schema_wraps_name_description_parameters():
    schema = tool_schema(TodoWriteTool())
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "todo_write"
    assert schema["function"]["description"]
    assert isinstance(schema["function"]["parameters"], dict)


def test_schema_preserves_params_json_schema():
    tool = TodoWriteTool()
    params = tool_schema(tool)["function"]["parameters"]
    assert params["properties"]["action"]["enum"] == ["add", "update", "list"]
    assert params["required"] == ["action"]


def test_schema_defaults_missing_type_to_object():
    class Bare(TodoWriteTool):
        @property
        def params_json_schema(self):
            return {"properties": {}}

    assert tool_schema(Bare())["function"]["parameters"]["type"] == "object"


def test_schemas_returns_one_per_tool():
    registry = ToolRegistry()
    registry.register(TodoWriteTool())
    schemas = tool_schemas(registry)
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "todo_write"


def test_schema_sorted_by_registry_order():
    registry = ToolRegistry()
    registry.register(TodoWriteTool())
    registry.register(_Named("aaa"))
    registry.register(_Named("zzz"))
    names = [s["function"]["name"] for s in tool_schemas(registry)]
    assert names == sorted(names)


def test_schemas_from_tools_preserves_order():
    a, b = _Named("bbb"), _Named("aaa")
    assert [s["function"]["name"] for s in tool_schemas_from_tools([a, b])] == ["bbb", "aaa"]


def test_schema_is_json_serializable():
    payload = json.dumps(tool_schema(TodoWriteTool()), ensure_ascii=False)
    assert '"todo_write"' in payload


class _Named(TodoWriteTool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name
