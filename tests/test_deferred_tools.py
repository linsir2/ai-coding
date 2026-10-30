"""Deferred tool schemas: advertised only after the model asks for them.

Measured 2026-10-30: 26 tools cost ~23,581 characters on every request, with the
expensive half used once in a while (memory_search 2,061, memory_write 1,554,
parallel_research 1,127, the job trio).  Deferring them costs one call --
``tool_search`` -- and the next request carries their schemas.

Loading is a surface change, never a permission: the posture and rule filters
still run at surface time, so a denied tool that is "loaded" never appears.
"""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.tool_search_tool import ToolSearchTool


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        return StepResult(text="ok", finish_reason="stop")

    async def aclose(self) -> None:
        return None


class _Tool(BaseTool):
    def __init__(self, name: str, *, defer: bool = False) -> None:
        self._name = name
        self.defer = defer

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} does {self._name} things"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def __repr__(self) -> str:  # the diff should name a tool, not an address
        return f"<tool {self._name}>"

    async def execute(self, params, ctx=None):  # type: ignore[override]
        return ToolResult(success=True, output="ok")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_Tool("read"))
    registry.register(_Tool("bash"))
    registry.register(_Tool("job_list", defer=True))
    registry.register(_Tool("job_kill", defer=True))
    registry.register(_Tool("memory_search", defer=True))
    registry.register(ToolSearchTool(registry))
    return registry


def _session(loaded: tuple[str, ...] = ()) -> SessionData:
    session = SessionData(
        session_id="s", title="", created_time="t", last_access_time="t"
    )
    session.loaded_tools = loaded
    return session


def _loop(registry: ToolRegistry) -> AgentLoop:
    return AgentLoop(
        _Engine(),
        SessionService.__new__(SessionService),
        registry,
        workspace=".",
        settings=AIConfig(stop_policy="natural", max_tool_iterations=3),
    )


def test_a_deferred_tool_is_not_in_the_surface_until_it_is_loaded():
    loop = _loop(_registry())

    names = {tool.name for tool in loop._surface_tools("normal", False)}

    assert "read" in names and "bash" in names
    assert "job_list" not in names, "a deferred tool must not pay its schema unasked"
    assert "tool_search" in names, "the way to ask has to be advertised"


def test_loading_a_tool_puts_it_in_the_surface():
    loop = _loop(_registry())

    names = {
        tool.name
        for tool in loop._surface_tools("normal", False, loaded=("job_list",))
    }

    assert "job_list" in names
    assert "job_kill" not in names, "loading one tool loads exactly that one"


def test_a_scoped_denial_still_hides_a_loaded_tool():
    loop = _loop(_registry())

    # The gate refuses the name outright; loading must not bypass it.
    loop.gate = _DenyingGate()
    names = {tool.name for tool in loop._surface_tools("normal", False, loaded=("job_list",))}

    assert "job_list" not in names


class _DenyingGate:
    def hides(self, name, **kwargs):  # type: ignore[no-untyped-def]
        return name == "job_list"

    def partially_allowed(self, name, **kwargs):  # type: ignore[no-untyped-def]
        return False


@pytest.mark.asyncio
async def test_tool_search_reports_matches_and_notes_them():
    registry = _registry()
    tool = ToolSearchTool(registry)
    seen: list[tuple[str, ...]] = []

    result = await tool.execute(
        {"query": "job"},
        ToolContext(
            note_tool_loaded=lambda names: seen.append(tuple(names)),
            searchable_names=("job_list", "job_kill", "memory_search"),
        ),
    )

    assert result.success
    assert "job_list" in result.output and "job_kill" in result.output
    assert seen == [("job_kill", "job_list")]


@pytest.mark.asyncio
async def test_tool_search_skips_already_loaded_tools():
    tool = ToolSearchTool(_registry())

    result = await tool.execute(
        {"query": "job"},
        ToolContext(
            loaded_tools=("job_list", "job_kill"),
            searchable_names=("job_list", "job_kill", "memory_search"),
        ),
    )

    assert "job_list" not in result.output
    assert "no deferred tool matched" in result.output


@pytest.mark.asyncio
async def test_tool_search_offers_only_what_this_requests_surface_allows():
    """Loading is a surface change: a tool the request may not call is not
    even on offer, so the search cannot widen the catalogue by asking."""
    tool = ToolSearchTool(_registry())

    result = await tool.execute(
        {"query": "memory"},
        ToolContext(searchable_names=("job_list", "job_kill")),
    )

    assert "no deferred tool matched" in result.output
    assert "memory_search" not in result.output
    assert "Still available to load: job_kill, job_list" in result.output, (
        "what is on offer is the surface's own deferred set"
    )


def test_surface_schemas_carries_only_what_the_session_loaded():
    """The cache key must include ``loaded_tools``: loading a tool has to change
    the very next request, not the one after a restart."""
    loop = _loop(_registry())
    session = _session()

    before = {schema["function"]["name"] for schema in loop._surface_schemas(session)}
    assert "job_list" not in before

    session.loaded_tools = ("job_list",)
    after = {schema["function"]["name"] for schema in loop._surface_schemas(session)}

    assert "job_list" in after


def test_searchable_names_is_the_surface_with_the_cost_filter_lifted():
    loop = _loop(_registry())
    session = _session()

    assert set(loop._searchable_names(session)) == {
        "job_list",
        "job_kill",
        "memory_search",
    }


def test_searchable_names_obeys_the_permission_rules():
    loop = _loop(_registry())
    session = _session()
    loop.gate = _DenyingGate()

    names = loop._searchable_names(session)

    assert "job_list" not in names, "a denied tool is not on offer"
    assert "job_kill" in names


def test_searchable_names_is_empty_when_the_posture_forbids_every_match():
    """A read-only request cannot load a mutating tool: the search must offer
    exactly the surface, not the registry."""
    loop = _loop(_registry())
    session = _session()
    session.readonly = True

    assert loop._searchable_names(session) == ()


def test_the_loaded_set_is_capped():
    loop = _loop(_registry())
    session = _session()

    loop._note_tools_loaded(session, tuple(f"tool_{index}" for index in range(20)))

    assert len(session.loaded_tools) == 12
    assert session.loaded_tools[-1] == "tool_19", "the oldest fall out first"


def test_loaded_tools_survive_a_round_trip():
    session = _session(("job_list", "memory_search"))

    data = session.to_dict()

    assert data["loadedTools"] == ["job_list", "memory_search"]
    assert SessionData.from_dict(data).loaded_tools == ("job_list", "memory_search")


def test_a_session_with_nothing_loaded_writes_no_key():
    assert "loadedTools" not in _session().to_dict()
