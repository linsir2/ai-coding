"""TDD (R1a-4): AgentLoop with the optional M3 components injected.

Migrated from ``test_agent_loop_m3.py``.  The intent is unchanged — prove that
compression, prompt assembly, skills and memory are wired into the turn — but the
driver is now a step-wise ``ChatEngine`` instead of the removed ``AIService``.
"""

from types import SimpleNamespace

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.project_instructions import ProjectInstructionsLoader
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage, ToolResult
from ai_coding.service.session_service import SessionService
from ai_coding.skills.registry import SkillRegistry
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    def __init__(self, steps=None) -> None:
        self._steps = list(steps or [StepResult(text="ok")])
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "instructions": instructions})
        return self._steps.pop(0) if self._steps else StepResult(text="ok")


class _TruncatingContext:
    """Fake compressor: keeps only the newest message."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def compress(self, messages):
        self.calls.append(list(messages))
        return list(messages)[-1:]


class _Memory:
    def __init__(self) -> None:
        self.recalled: list = []
        self.remembered: list = []
        self.harvested: list = []

    # R3b contract: memory is *not* part of the system prompt any more.  It reaches
    # the request as a volatile tail block, and the write-back is a delta harvest.
    def index_notes(self):
        return [SimpleNamespace(id="n1", kind=SimpleNamespace(value="decision"), text="prior note")]

    def render_index_block(self, notes):
        self.recalled.append(list(notes))
        return "<memory-constraints>\n- [decision#n1] prior note\n</memory-constraints>"

    async def harvest(self, messages, **kwargs):
        self.harvested.append([message.content for message in messages])
        self.remembered.append(kwargs.get("summary"))
        return SimpleNamespace(added=(), updated=(), superseded=(), skipped=())

    async def consolidate(self, **kwargs):
        return SimpleNamespace(merged=(), promoted=(), expired=(), evicted=())


class _Tool(BaseTool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        return ToolResult(success=True, output="ok")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_Tool("read"))
    return registry


@pytest.mark.asyncio
async def test_compression_applied_when_context_injected(tmp_path):
    engine = _Engine()
    sessions = SessionService(tmp_path / "sessions")
    context = _TruncatingContext()
    loop = AgentLoop(
        engine,
        sessions,
        _registry(),
        context=context,  # type: ignore[arg-type]
        settings=AIConfig(stop_policy="natural"),
    )
    session = sessions.create()
    session.add_message(ChatMessage(role="user", content="pad one", timestamp="t"))
    session.add_message(ChatMessage(role="assistant", content="pad two", timestamp="t"))

    await loop.process_input(session, "go")

    sent = engine.requests[0]["messages"]
    assert context.calls  # compress was consulted
    assert sent is not session.messages  # the loop never hands over the live list
    assert not [m for m in sent if "pad" in (m.content or "")]
    assert any(m.content == "go" for m in sent)


@pytest.mark.asyncio
async def test_instructions_assembled_when_prompt_injected(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill for testing\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "CLAUDE.md").write_text("respect the build", encoding="utf-8")
    engine = _Engine()
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(
        engine,
        sessions,
        _registry(),
        prompt=PromptAssembler("base instructions"),
        skills=SkillRegistry(tmp_path / "skills"),
        project=ProjectInstructionsLoader(tmp_path),
        settings=AIConfig(stop_policy="natural"),
    )

    await loop.process_input(sessions.create(), "go")

    instructions = engine.requests[0]["instructions"]
    assert instructions is not None
    assert "base instructions" in instructions
    assert "demo skill for testing" in instructions
    assert "respect the build" in instructions


@pytest.mark.asyncio
async def test_memory_is_injected_at_the_tail_and_harvested(tmp_path):
    engine = _Engine()
    sessions = SessionService(tmp_path / "sessions")
    memory = _Memory()
    loop = AgentLoop(
        engine,
        sessions,
        _registry(),
        prompt=PromptAssembler("base"),
        memory=memory,  # type: ignore[arg-type]
        settings=AIConfig(stop_policy="natural"),
    )

    await loop.process_input(sessions.create(), "go")

    assert memory.recalled  # the constraint block was built
    assert "prior note" not in (engine.requests[0]["instructions"] or "")
    tail = "\n".join(
        message.content or "" for message in engine.requests[0]["messages"][-2:]
    )
    assert "prior note" in tail  # ... but it is in the tail block
    assert memory.harvested  # the turn delta was harvested at turn end


@pytest.mark.asyncio
async def test_components_absent_behaves_like_bare_loop(tmp_path):
    engine = _Engine()
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(engine, sessions, _registry())
    session = sessions.create()

    text = await loop.process_input(session, "go")

    assert text == "ok"
    assert engine.requests[0]["instructions"] is None
    assert [m.role for m in session.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_a_denied_skill_is_hidden_from_the_model_catalog(tmp_path):
    """r16/E-5: deny is an operator boundary — the model is not told the skill
    exists (the gate and the mention block withhold it too)."""
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill for testing\n---\nbody\n",
        encoding="utf-8",
    )
    engine = _Engine()
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(
        engine,
        sessions,
        _registry(),
        prompt=PromptAssembler("base"),
        skills=SkillRegistry(tmp_path / "skills"),
        settings=AIConfig(stop_policy="natural", skill_permissions={"demo": "deny"}),
    )

    await loop.process_input(sessions.create(), "go")

    assert "demo skill for testing" not in (engine.requests[0]["instructions"] or "")
