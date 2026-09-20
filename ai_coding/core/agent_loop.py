"""The agent main loop: orchestrate a single conversation turn.

Flow (M1): append user msg -> run AI over full history -> append assistant msg ->
clean tool pairing -> persist. Hooks (delta handlers) plug in via ``on_delta``.

M3 integrates optional components (all default-None so behaviour is unchanged):
context compression, prompt assembly, project instructions, skills catalog, and
memory.  When absent the loop behaves exactly as in M2 (backwards compatible).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ai_coding.ai.base import AIService
from ai_coding.domain.run import assistant_message, tool_message, user_message
from ai_coding.domain.session import SessionData
from ai_coding.infra.time_utils import now_utc
from ai_coding.service.session_service import SessionService

if TYPE_CHECKING:
    from ai_coding.ai.prompt import PromptAssembler
    from ai_coding.core.context_compression import ContextManager
    from ai_coding.core.project_instructions import ProjectInstructionsLoader
    from ai_coding.memory.service import MemoryService
    from ai_coding.skills.registry import SkillRegistry

DeltaSink = Callable[[str], None]


class AgentLoop:
    """Owns the single-turn conversation lifecycle and its persistence."""

    def __init__(
        self,
        ai: AIService,
        sessions: SessionService,
        *,
        context: ContextManager | None = None,
        memory: MemoryService | None = None,
        prompt: PromptAssembler | None = None,
        skills: SkillRegistry | None = None,
        project: ProjectInstructionsLoader | None = None,
    ) -> None:
        self.ai = ai
        self.sessions = sessions
        self.context = context
        self.memory = memory
        self.prompt = prompt
        self.skills = skills
        self.project = project

    async def process_input(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
    ) -> str:
        session.add_message(user_message(user_input, now_utc()))

        # Compress the assembled history (on a copy) before handing it to the model.
        history = session.messages
        compact = history
        if self.context is not None:
            compact = await self.context.compress(history)

        # Assemble the system prompt: base + project instructions + skills + memory.
        instructions = None
        if self.prompt is not None:
            instructions = await self._build_instructions(compact)

        kwargs: dict[str, Any] = {"token_sink": on_delta}
        if self.prompt is not None:
            kwargs["instructions"] = instructions
        turn = await self.ai.execute_turn(compact, user_input, **kwargs)

        session.add_message(assistant_message(turn, now_utc()))
        for out in turn.tool_outputs:
            session.add_message(tool_message(out, now_utc()))
        session.ensure_tool_pairing()
        self.sessions.save(session)

        # Silent memory upkeep: never raises, never blocks the turn.
        await self._remember(session.messages, turn.text)

        return turn.text or ""

    # ---- optional component wiring ---------------------------------------

    async def _build_instructions(self, compact: list[Any]) -> str | None:
        """Return the single assembled instruction string, or None when unused."""
        memory_catalog = ""
        if self.memory is not None:
            memory_catalog = "\n".join(await self.memory.recall(compact))
        if self.prompt is None:
            return None
        project_text = self.project.load() if self.project is not None else ""
        skills_catalog = self.skills.catalog_text() if self.skills is not None else ""
        return self.prompt.build(
            project_instructions=project_text,
            skills_catalog=skills_catalog,
            memory_catalog=memory_catalog,
        )

    async def _remember(self, messages: list[Any], conclusion: str | None) -> None:
        if self.memory is None:
            return
        try:
            await self.memory.remember(messages, conclusion)
        except Exception:
            pass  # memory is best-effort


__all__ = ["AgentLoop", "DeltaSink"]
