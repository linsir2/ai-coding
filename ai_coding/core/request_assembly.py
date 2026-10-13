"""Assembling one iteration's request (R7a).

`request_builder` owns the *shape* of a request (the tail is a declared, ordered
list of volatile blocks); this unit owns the *content* of one: it compresses the
history, builds the static instructions, folds the task list in where the policy
wants it, and asks memory and the digest ring for their blocks.

That is worth its own name because two rules live here and nowhere else:

* **order is a contract** — memory / task / hook / digest, never sorted, never
  merged, so ``ContextAssembled.tail_blocks`` can be read against the messages;
* the digest ring and the memory block are **conditional** — the ring only pays
  for itself when this request's history was actually rewritten (R6a), and the
  memory block is announced once per turn, not once per iteration (R3).

R7a moved this out of ``process_input``, which had grown to 228 lines because
every new block was another ten lines inside the same method.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ai_coding.core.compaction import Compactor
from ai_coding.core.events import ContextCompacted, LoopEvent, MemoryInjected
from ai_coding.core.request_builder import build_tail, content_of, volatile_block
from ai_coding.core.sop_router import SOPRouter
from ai_coding.core.turn_books import TurnBooks
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import render_working_memory
from ai_coding.infra.deadline import Deadline
from ai_coding.skills.registry import SkillRegistry, resolve_mentions

__all__ = ["Assembled", "RequestAssembler"]


def _observed_prompt_tokens(session: SessionData) -> int:
    """The provider's own count for the newest turn that reported one.

    The compaction gate used to read only ``estimate_tokens``, which counts
    characters and underestimated a tool-output-heavy history by ~4x on a real
    session (60k estimated against 237,157 billed). The provider's number is the
    ground truth, so it is what the gate gets. 0 means nothing has been measured
    yet, and the gate falls back to the estimate exactly as before.
    """
    usages = getattr(session, "turn_usages", None) or []
    return int(getattr(usages[-1], "prompt_tokens", 0) or 0) if usages else 0


@dataclass(frozen=True)
class Assembled:
    """One iteration's request, ready for the engine."""

    history: list[ChatMessage]
    instructions: str | None
    request: list[ChatMessage]
    #: The canonical form of this request, which the next iteration compares against.
    previous_request: str


class RequestAssembler:
    """Build one request per iteration, from the turn's own state."""

    def __init__(
        self,
        *,
        compactor: Any,
        policy: Any,
        memory: Any,
        reporter: Any,
        build_instructions: Callable[
            [Deadline | None, SessionData], Awaitable[str | None]
        ] | None = None,
        # M2: the MCP catalog, rebuilt per step from live manager state.  A
        # callable rather than a string because it must never be frozen into a
        # schema: a reconnected server's new tools have to show up on the very
        # next step.
        catalog_builder: Callable[[], str] | None = None,
        settings: Any,
        emit: Callable[[LoopEvent], None],
        skills: SkillRegistry | None = None,
    ) -> None:
        self._compactor = compactor
        self._policy = policy
        self._memory = memory
        self._reporter = reporter
        # The system prompt belongs to the composition root (project instructions +
        # the skills catalogue), so the loop hands its builder in rather than the
        # assembler learning about prompt assembly.
        self._build_instructions = build_instructions
        self._catalog_builder = catalog_builder
        self._settings = settings
        self._emit = emit
        # M2: SOP router for recommending skills based on current context.
        self._router = SOPRouter(skills) if skills is not None else None
        # Kept beside the router so an explicitly named skill can be resolved
        # against the same registry the automatic recommendation uses.
        self._skills = skills

    async def assemble(
        self,
        *,
        session: SessionData,
        task: TaskState,
        books: TurnBooks,
        turn_number: int,
        hook_context: str,
        deadline: Deadline | None,
        index: int,
        previous_request: str | None,
        pinned: Sequence[ChatMessage] = (),
    ) -> Assembled:
        """Compress, instruct, inject, tail — in that order, every iteration."""
        # I3: capture anchor before compaction
        if self._router is not None:
            constraints = self._get_active_constraints(session)
            self._router.capture_pre_compaction_anchor(session, task, constraints)
        history = await self._compactor.compress(
            session.messages,
            deadline=deadline,
            on_summary=books.summaries.append,
            pinned=pinned,
            **self._compactor_extras(session),
        )
        # R6a: the ring only pays for itself when this request's history was
        # actually rewritten — otherwise the recent turns are still there
        # (docs/r6-analysis.md 2.1, D-R6-2).
        compacted = Compactor.did_compact(session.messages, history)
        if compacted and not books.compaction_announced:
            self._emit(
                ContextCompacted(
                    before_messages=len(session.messages),
                    after_messages=len(history),
                    before_chars=sum(len(m.content or "") for m in session.messages),
                    after_chars=sum(len(m.content or "") for m in history),
                )
            )
            books.compaction_announced = True
        instructions = (
            await self._build_instructions(deadline, session)
            if self._build_instructions is not None
            else None
        )
        injection = self._policy.inject_task(task, instructions)
        instructions = injection.instructions
        # Memory and task state are *volatile tail blocks*: the system prompt stays
        # byte-identical and the history stays append-only, so only this small
        # region diverges between turns (docs/r2 §1.13).
        memory_block, memory_ids = self._memory.block()
        if memory_block and not books.memory_announced:
            self._emit(MemoryInjected(note_ids=memory_ids, chars=len(memory_block)))
            books.memory_announced = True
        # I2: track constraint injection
        if memory_block and self._router is not None:
            # Extract individual constraints from the block
            constraints = [
                line.strip("- ").strip()
                for line in memory_block.split("\n")
                if line.strip().startswith("- ")
            ]
            self._router.record_constraint_injection(constraints, session)
        digest_block = render_working_memory(
            session.turn_digests,
            current_turn=turn_number,
            max_chars=int(getattr(self._settings, "working_memory_chars", 1200)),
            compacted=compacted,
        )
        # G8-1: the one block that tells the model its budget is nearly gone.
        # Last, after the digest, because it is an instruction and instructions
        # are the thing recency helps -- everything above it is state to read,
        # this is the one line asking for a change of behaviour.
        pressure = self._policy.budget_pressure(
            iteration=index, tokens_spent=self._reporter.tokens_spent()
        )
        # M2: recommend skills based on current context (after task, before hook).
        recommended_skills_block = ""
        recovery_block = ""
        if self._router is not None:
            user_input = self._extract_user_input(session)
            recent_tools = self._extract_recent_tools(session, books)
            task_goal = task.goal if task else ""
            recent_failures = [f[1] for f in books.failures]
            observations = getattr(session, "tool_observations", None)
            # F1: check for cached recovery
            recovery = self._router.check_recovery(recent_failures, session)
            if recovery:
                recovery_block = (
                    "<error-recovery>\n"
                    f"之前遇到类似错误时，这个方法是有效的：\n{recovery}\n"
                    "</error-recovery>"
                )
            # F2: compute current phase
            phase = self._compute_phase(session, books, task)
            # F3: check for intervention
            intervention = self._router.intervene(
                recent_failures, observations, session,
                getattr(session, "skill_stats", None),
            )
            if intervention.mode == "hard":
                recommended_skills_block = self._format_intervention(intervention)
            else:
                matches = self._router.route(
                    user_input, recent_tools, task_goal, recent_failures,
                    observations=observations,
                    turn_number=turn_number,
                    stats=getattr(session, "skill_stats", None),
                    phase=phase,
                )
                recommended_skills_block = SOPRouter.format_block(matches)
                # F3: soft intervention appends pattern_dna
                if intervention.mode == "soft":
                    recommended_skills_block += (
                        "\n\n<skill-pattern>\n"
                        f"[{intervention.skill_name}] {intervention.content}\n"
                        "</skill-pattern>"
                    )
            # A skill the human named is the same kind of signal, stated by a
            # person rather than a heuristic, so it rides the same block. The
            # TUI writes `@name` into the composer; without this the name is
            # decoration that reaches the model as prose.
            requested = resolve_mentions(user_input, self._skills)
            if requested:
                named = ", ".join(f'name="{name}"' for name in requested)
                recommended_skills_block += (
                    "\n\n<requested-skills>\n"
                    "用户显式指定了以下 skill，动手前先读取："
                    f'skill(action="read", {named})。\n'
                    "</requested-skills>"
                )
        # I3: inject anchor after compaction
        anchor_block = self._format_anchor_block(session, compacted)
        # M2: the catalog sits with the other state to read, before the
        # instructions — it is reference data, not an instruction.
        catalog = self._catalog_builder() if self._catalog_builder is not None else ""
        tail, tail_blocks = build_tail(
            [
                volatile_block("memory", memory_block),
                volatile_block("mcp_catalog", catalog),
                volatile_block("task", content_of(injection.message)),
                volatile_block("recommended_skills", recommended_skills_block),
                volatile_block("error_recovery", recovery_block),
                volatile_block("anchor", anchor_block),
                volatile_block("hook", hook_context),
                volatile_block("digest", digest_block),
                volatile_block("budget", pressure),
            ]
        )
        request = [*history, *tail]
        canonical = self._reporter.report_context(
            iteration=index,
            instructions=instructions,
            history=history,
            request=request,
            tail_blocks=tail_blocks,
            previous=previous_request,
        )
        return Assembled(
            history=history,
            instructions=instructions,
            request=request,
            previous_request=canonical,
        )

    # ---- helpers (M2) -------------------------------------------------------

    @staticmethod
    def _extract_user_input(session: SessionData) -> str:
        """Get the last user message content."""
        for m in reversed(session.messages):
            if m.role == "user":
                return (m.content or "").strip()
        return ""

    @staticmethod
    def _extract_recent_tools(session: SessionData, books: TurnBooks) -> list[str]:
        """Get recently used tools from books + message scan."""
        tools = set(getattr(books, "tool_success", set()) or set())
        for m in session.messages[-10:]:
            if m.role == "assistant" and m.tool_calls:
                for call in m.tool_calls:
                    tools.add(call.name)
        return list(tools)

    # ---- F2: Phase computation ----------------------------------------------

    @staticmethod
    def _compute_phase(session: SessionData, books: Any, task: Any) -> str:
        """Determine current task phase.

        Returns one of: "planning", "executing", "debugging", "reviewing".
        """
        # Debugging takes priority — repeated failures need debugging skills
        # Lowered threshold: 1 turn of failures is enough to trigger debugging
        if getattr(books, "failed_streak", 0) >= 1:
            return "debugging"
        # If there's an active task with items
        if task is not None and task.items:
            # Check if any item has a verify command (reviewing phase)
            if any(item.verify for item in task.items if item.status != "completed"):
                return "reviewing"
            return "executing"
        return "planning"

    # ---- F3: Intervention formatting ----------------------------------------

    @staticmethod
    def _format_intervention(intervention: Any) -> str:
        """Format a hard intervention as a tail block."""
        return (
            "<skill-intervention>\n"
            f"检测到连续失败，以下是 [{intervention.skill_name}] 的修复步骤：\n\n"
            f"{intervention.content}\n"
            "</skill-intervention>"
        )

    # ---- I3: Anchor block ---------------------------------------------------

    @staticmethod
    def _format_anchor_block(session: SessionData, compacted: bool) -> str:
        """Format pre-compaction anchor as tail block."""
        anchor = getattr(session, "pre_compaction_anchor", "")
        if not compacted or not anchor:
            return ""
        return (
            "<task-anchor>\n"
            f"压缩前的任务锚点：{anchor}\n"
            "如果当前工作偏离此目标，请回到原始任务。\n"
            "</task-anchor>"
        )

    # ---- I2: Constraint extraction ------------------------------------------

    def _compactor_extras(self, session: SessionData) -> dict[str, Any]:
        """The compaction arguments this particular compactor can accept.

        ``compactor`` is injected as ``Any`` and an older one may only implement
        the pre-existing signature. The new arguments are additive — the
        compaction boundary and the provider's token count — so a compactor that
        has not heard of them keeps working instead of raising, exactly as this
        module already prefers ``compress_if_needed`` over ``compress``.
        """
        wanted = {
            "session": session,
            "observed_tokens": _observed_prompt_tokens(session),
        }
        try:
            accepted = inspect.signature(self._compactor.compress).parameters
        except (TypeError, ValueError):  # pragma: no cover — not introspectable
            return {}
        return {name: value for name, value in wanted.items() if name in accepted}

    def _get_active_constraints(self, session: SessionData) -> list[str]:
        """Get currently active memory constraints from session tracking."""
        tracking = getattr(session, "constraint_tracking", {})
        return [c for c, t in tracking.items() if t.get("injected")]
