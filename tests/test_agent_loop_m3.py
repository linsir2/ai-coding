"""TDD (M3-3): AgentLoop integrates context/memory/prompt/skills/project.

Uses a FakeAI that records the ``instructions`` it received, plus real M3
components (ContextManager, MemoryService, PromptAssembler, SkillRegistry,
ProjectInstructionsLoader) wired so we can assert the data flow.
"""

from types import SimpleNamespace

import pytest

from ai_coding.ai.base import AIService
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.context_compression import ContextManager
from ai_coding.core.project_instructions import ProjectInstructionsLoader
from ai_coding.domain.run import TurnResult
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore
from ai_coding.service.session_service import SessionService
from ai_coding.skills.registry import SkillRegistry


class FakeRecordingAI(AIService):
    def __init__(self):
        self.last_history = None
        self.last_instructions = None

    async def execute_turn(self, history, user_input, token_sink=None, *, instructions=None):
        self.last_history = list(history)
        self.last_instructions = instructions
        return TurnResult(text="ok")


def _make(tmp_path, **loop_kwargs):
    ai = FakeRecordingAI()
    svc = SessionService(tmp_path / "sessions")
    loop = AgentLoop(ai, svc, **loop_kwargs)
    return ai, svc, loop


@pytest.mark.asyncio
async def test_instructions_passed_to_ai(tmp_path):
    prompt = PromptAssembler("base prompt")
    ai, svc, loop = _make(tmp_path, prompt=prompt)
    session = svc.create()
    await loop.process_input(session, "hello")
    assert ai.last_instructions is not None
    assert "base prompt" in ai.last_instructions


@pytest.mark.asyncio
async def test_compression_applied_to_history(tmp_path):
    settings = SimpleNamespace(
        max_context_tokens=2,
        max_messages=3,
        snip_keep_head=1,
        snip_keep_tail=1,
        keep_recent_tool_results=1,
        per_result_persist_bytes=100,
        l4_keep_tail=1,
    )
    ai, svc, loop = _make(
        tmp_path,
        context=ContextManager(settings, transcripts_root=tmp_path, summarizer=None),
    )
    session = svc.create()
    # Pre-populate a long history via repeated turns that produce no text? Instead
    # directly append messages to the session before the loop compresses them.
    from ai_coding.domain.message import ChatMessage

    for i in range(10):
        session.add_message(ChatMessage(role="user", content=f"m{i}", timestamp="t"))
    await loop.process_input(session, "now")
    assert len(ai.last_history) <= 3  # compressed before being handed to the AI
    assert ai.last_history is not session.messages


@pytest.mark.asyncio
async def test_project_and_skills_catalog_in_instructions(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("respect the build", encoding="utf-8")
    _write_skill(tmp_path / "skills", "bash", "run a shell")
    project = ProjectInstructionsLoader(tmp_path)
    skills = SkillRegistry(tmp_path / "skills")
    prompt = PromptAssembler("base")
    ai, svc, loop = _make(tmp_path, prompt=prompt, skills=skills, project=project)
    session = svc.create()
    await loop.process_input(session, "hi")
    assert "respect the build" in ai.last_instructions
    assert "- bash" in ai.last_instructions


@pytest.mark.asyncio
async def test_memory_remember_called_and_recalled(tmp_path):
    store = MemoryStore(tmp_path / ".memory")
    memory = MemoryService(
        store,
        extractor=None,
        config=SimpleNamespace(
            enabled=True,
            consolidate_threshold=100,
            max_index_entries=200,
            max_per_turn_injections=3,
        ),
    )
    prompt = PromptAssembler("base")
    ai, svc, loop = _make(tmp_path, memory=memory, prompt=prompt)
    session = svc.create()
    await loop.process_input(session, "remember the fix: pin requests")
    assert store.count >= 1  # remember() persisted something


@pytest.mark.asyncio
async def test_components_optional_backwards_compatible(tmp_path):
    ai, svc, loop = _make(tmp_path)  # no M3 components
    session = svc.create()
    text = await loop.process_input(session, "hi")
    assert text == "ok"
    assert ai.last_instructions is None


def _write_skill(root, name, description):
    from pathlib import Path

    p = Path(root)
    (p / name).mkdir(parents=True, exist_ok=True)
    (p / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody",
        encoding="utf-8",
    )
