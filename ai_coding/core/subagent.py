"""Sub-agent orchestration (R1a-5).

A sub-agent is a self-contained run of the **same** loop the parent uses: it gets
the parent turn's request view and system prompt as its starting context, its own
task list, its own (sub-agent-free) tool stack, and — when the workspace is a git
repo and isolation is on — its own worktree.  Its reasoning is discarded; it
returns a structured packet which is also written to a ledger file.

Design notes (docs/r1a-5-analysis.md):
  - the semaphore is taken *before* the worktree, so the concurrency cap also
    bounds the number of worktrees in flight;
  - timeouts are a status, not an exception: they must be reportable to the
    parent so it can decide what to do next;
  - a sub-agent must not litter the user's ``sessions/`` directory.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import BackgroundTaskDone
from ai_coding.core.guard import build_guards
from ai_coding.core.observers import ScopedObserver
from ai_coding.core.turn_report import report_degraded
from ai_coding.core.worktree import WorktreeError
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.subagent import (
    BackgroundTask,
    ParentContext,
    SubagentResult,
    extract_json_facts,
)
from ai_coding.infra.deadline import DeadlineExpired, run_with_deadline
from ai_coding.infra.json_utils import to_json_pretty
from ai_coding.infra.time_utils import now_utc
from ai_coding.skills.registry import SkillRegistry
from ai_coding.tools.registry import ToolRegistry

EngineFactory = Callable[[], ChatEngine]
BuildTools = Callable[[str | None], ToolRegistry]

_log = logging.getLogger(__name__)

# TurnStatus value -> packet status
_STATUS_MAP: dict[str, str] = {
    "completed": "completed",
    "max_iterations": "max_iterations",
    "timeout": "timeout",
    # R4b: running out of token budget is partial work, like running out of
    # iterations — not a failure.  Omitting it would read as "error".
    "budget": "max_iterations",
    "stopped": "error",
    "aborted": "error",
    "error": "error",
}

# Fraction of the sub-agent's budget the *inner* turn deadline may use; the rest
# is left for finalisation and for the outer backstop (R2a).
_INNER_BUDGET_RATIO = 0.9

#: Completion summary tail for the announce event (R1-2): the full packet
#: lives in the ledger file; the event carries a pointer, not the prose.
_SUMMARY_TAIL = 200

#: Background table cap (R1-2): a long-lived process must not grow the table
#: without bound.  Running rows are never pruned; the oldest terminal rows go
#: first.  Restarts lose the whole table anyway (documented, Hermes rule).
_MAX_BACKGROUND_ROWS = 200


class _NullSessions:
    """Swallows saves: a sub-agent's transcript is its ledger, not a session file."""

    def save(self, session: SessionData) -> None:
        return None

    def mark_pending_approval(
        self, session_id: str, request: dict[str, Any] | None
    ) -> None:
        """P3: a sub-agent has no session file to mark, and nobody to ask
        anyway -- its approval callback is the auto-approver."""
        return None


class SubAgentRunner:
    """Run one sub-agent task, optionally inside a git worktree sandbox."""

    def __init__(
        self,
        app_config: Any,
        engine_factory: EngineFactory,
        build_tools: BuildTools,
        *,
        ledger_root: str | Path | None = None,
        max_concurrent: int = 3,
        timeout_seconds: float = 600.0,
        settings: AIConfig | None = None,
        approval: Any | None = None,
        workspace: str = ".",
        observer: Any | None = None,
        base_instructions: str = "",
        skills: SkillRegistry | None = None,
    ) -> None:
        self._app_config = app_config
        self._skills = skills
        # M2 fix: the guard chain used to be assembled only for the parent loop,
        # so every sub-agent got ``AgentLoop(guards=None)`` -- which the
        # dispatcher turns into an empty chain, i.e. no red lines, no path
        # strictness, no duplicate-call budget.  A worktree isolates *files*; it
        # never relaxed the policy, and a child inherits the parent's policy
        # rather than a copy of the parent's turn counters.
        self._guards = build_guards(app_config)
        self._engine_factory = engine_factory
        self._build_tools = build_tools
        self._ledger_root = Path(ledger_root) if ledger_root is not None else None
        self._timeout = float(timeout_seconds)
        self._settings = settings or AIConfig()
        # No human is present inside a sub-agent to answer a WARN prompt; the DENY
        # tier still blocks dangerous calls regardless of the callback.
        from ai_coding.security.approval import AutoApproveCallback

        self._approval = approval if approval is not None else AutoApproveCallback()
        self._workspace = workspace
        # R4a: the parent's observer, so a sub-agent stops being a black box.  Its
        # events are wrapped with their scope/parent by ``ScopedObserver`` below.
        self._observer = observer
        # R7e-4: what a ``task_only`` child gets instead of the parent's prompt.
        self._base_instructions = str(base_instructions or "")
        self._max_concurrent_n = max(1, int(max_concurrent))
        self._semaphore = asyncio.Semaphore(self._max_concurrent_n)
        # R1-2: background runs tracked across turns: run id -> row, plus the
        # live asyncio tasks (strong refs so they survive the spawning turn),
        # plus the announce context (emit/origin/name) keyed the same way.
        self._background: dict[str, BackgroundTask] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._announce: dict[str, tuple[Any, str, str]] = {}

    @property
    def timeout_seconds(self) -> float:
        """The budget the tool layer advertises as this runner's per-call ceiling."""
        return self._timeout

    @property
    def context_mode(self) -> str:
        """``full`` (the parent's view) or ``task_only`` (a clean window), R7e-4."""
        return str(getattr(self._settings, "subagent_context_mode", "full") or "full")

    async def run(
        self,
        task: str,
        name: str = "sub-agent",
        *,
        worktree_manager: Any = None,
        isolate: bool = True,
        parent: ParentContext | None = None,
        parent_span: str = "",
        observer: Any | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> SubagentResult:
        """Run ``task`` and return its packet.  Never raises for a sub-agent failure.

        ``observer`` is a per-call override for the constructor's observer:
        the tool layer fans the parent's emit through here, so child events
        reach the parent trace (previously the slot was never filled and the
        child ran as a black box — measured: zero scoped events over 23 runs).

        ``output_schema`` (R1-3) is a JSON-shape contract for the conclusion:
        the child is instructed to return its verdict as JSON, and the packet
        carries the parsed ``facts`` plus a ``schema_valid`` verdict.  The raw
        conclusion always survives — a bad brace must not kill an hour of work.
        """
        active = self._observer if observer is None else observer
        async with self._semaphore:
            return await self._run_isolated(
                task, name, parent, parent_span, active,
                worktree_manager, isolate, output_schema,
            )

    async def _run_isolated(
        self,
        task: str,
        name: str,
        parent: ParentContext | None,
        parent_span: str,
        observer: Any | None,
        worktree_manager: Any | None,
        isolate: bool,
        output_schema: dict[str, Any] | None = None,
    ) -> SubagentResult:
        """Worktree branch shared by the synchronous and background paths."""
        if (
            isolate
            and worktree_manager is not None
            and await worktree_manager.can_isolate()
        ):
            try:
                async with worktree_manager.temporary(name) as wt:
                    return await self._run_in(
                        task, name, str(wt.path), parent,
                        parent_span=parent_span, observer=observer,
                        output_schema=output_schema,
                    )
            except WorktreeError:
                # Scoped like the child's own events: this record says the child
                # ran in the *main* workspace, and `report_degraded` carries no
                # name, so without the wrapper nothing ties it to a child — and
                # up to three run at once.
                report_degraded(
                    self._scoped_observer(name, parent_span, observer),
                    "worktree_isolate",
                    "the worktree could not be created",
                    "ran in the main workspace instead",
                )
        return await self._run_in(
            task, name, None, parent, parent_span=parent_span, observer=observer,
            output_schema=output_schema,
        )

    # ---- background runs (R1-2) -------------------------------------------

    def spawn_background(
        self,
        task: str,
        name: str = "sub-agent",
        *,
        worktree_manager: Any = None,
        isolate: bool = True,
        parent: ParentContext | None = None,
        parent_span: str = "",
        observer: Any | None = None,
        emit: Any = None,
        origin_session: str = "",
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        """Start a child without waiting; return its run id at once.

        The spawning turn pays nothing past this call — the child runs on
        under the same semaphore, timeout and ledger discipline as a
        synchronous run, and its completion lands as a ``BackgroundTaskDone``
        event on ``emit`` plus a row in :meth:`task_list`.

        A saturated pool rejects loudly (``RuntimeError``) instead of
        queuing silently: the turn must not pay for work that never starts.
        (Check-then-act races degrade to background queuing, never to
        turn-blocking — the semaphore is still the true bound.)  Must be
        called from async context: the supervised task is created on the
        running loop.
        """
        if self._semaphore.locked():
            raise RuntimeError(
                "sub-agent concurrency exhausted: "
                f"{self._max_concurrent()} background slots busy; "
                "retry later or raise ai.max_concurrent_subagents"
            )
        run_id = uuid.uuid4().hex[:12]
        active = self._observer if observer is None else observer
        self._background[run_id] = BackgroundTask(
            run_id=run_id, name=name, task=task, status="running",
            started=time.monotonic(),
        )
        self._prune_background()
        background_task = asyncio.get_running_loop().create_task(
            self._supervised(
                run_id, task=task, name=name, worktree_manager=worktree_manager,
                isolate=isolate, parent=parent, parent_span=parent_span,
                observer=active, emit=emit, origin_session=origin_session,
                output_schema=output_schema,
            ),
            name=f"subagent:{run_id}",
        )
        self._tasks[run_id] = background_task
        # A task cancelled before its first step never runs its body — the
        # supervised path below would never settle it.  The reaper closes
        # that hole: whatever state the row is still in, it gets settled.
        background_task.add_done_callback(functools.partial(self._reap, run_id))
        # The announce context rides alongside (the frozen row cannot hold it).
        self._announce[run_id] = (emit, origin_session, name)
        return run_id

    def cancel(self, run_id: str) -> bool:
        """Stop a running background task.  False when unknown or settled."""
        row = self._background.get(run_id)
        background_task = self._tasks.get(run_id)
        if row is None or background_task is None or row.status != "running":
            return False
        background_task.cancel()
        return True

    def resolve_run_id(self, prefix: str) -> str | None:
        """The full run id a printed prefix stands for, or None.

        ``/tasks`` shows ``run_id[:12]`` and ``cancel`` needs the whole thing;
        without this bridge the operator can only copy something that does not
        work.  Ambiguous prefixes resolve to None (the caller lists candidates)
        — the same rule ``/resume`` follows via ``SessionService.resolve``.
        """
        if not prefix:
            return None
        exact = self._background.get(prefix)
        if exact is not None:
            return prefix
        matches = [key for key in self._background if key.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def task_list(self) -> list[BackgroundTask]:
        """Background rows, oldest first (running and settled alike)."""
        return sorted(self._background.values(), key=lambda row: row.started)

    def _max_concurrent(self) -> int:
        return self._max_concurrent_n

    def _prune_background(self) -> None:
        """Bound the table: oldest terminal rows go first, running never."""
        terminal = sorted(
            (row for row in self._background.values() if row.status != "running"),
            key=lambda row: row.started,
        )
        while len(self._background) > _MAX_BACKGROUND_ROWS and terminal:
            self._background.pop(terminal.pop(0).run_id, None)

    async def _supervised(
        self,
        run_id: str,
        *,
        task: str,
        name: str,
        worktree_manager: Any | None,
        isolate: bool,
        parent: ParentContext | None,
        parent_span: str,
        observer: Any | None,
        emit: Any,
        origin_session: str,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        """Run one background child to settlement, then announce it.

        Never raises: an unretrieved task exception is noise at shutdown, so
        every failure mode below ends in a table row plus (best-effort) event.
        """
        try:
            async with self._semaphore:
                result = await self._run_isolated(
                    task, name, parent, parent_span, observer,
                    worktree_manager, isolate, output_schema,
                )
        except asyncio.CancelledError:
            result = SubagentResult(
                status="cancelled", error="cancelled by user",
                truncated=True, exit_reason="cancelled",
            )
        except Exception as exc:  # noqa: BLE001 — _run_in promises never-raise; belt.
            result = SubagentResult(
                status="error", error=f"supervisor: {exc}",
                truncated=True, exit_reason="supervisor_error",
            )
        self._finish_background(run_id, result, emit, origin_session)

    def _finish_background(
        self, run_id: str, result: SubagentResult, emit: Any, origin_session: str
    ) -> None:
        """Settle the row, drop the task ref, announce best-effort."""
        row = self._background.get(run_id)
        self._tasks.pop(run_id, None)
        self._announce.pop(run_id, None)
        if row is None or row.status != "running":
            return
        self._background[run_id] = replace(
            row, status=result.status, ended=time.monotonic(),
            error=result.error, usage=result.usage,
        )
        if emit is None:
            return
        # F4: write trace note for future sub-agents
        _write_trace_note(result, self._ledger_root)
        summary = (result.conclusion or result.error or "").strip().replace("\n", " ")
        if len(summary) > _SUMMARY_TAIL:
            summary = summary[:_SUMMARY_TAIL] + "…"
        try:
            emit(BackgroundTaskDone(
                run_id=run_id, name=row.name, status=result.status,
                summary=summary, truncated=result.truncated,
                origin_session=origin_session,
                # R1-3: the pointer the event docstring always promised — the
                # full packet (facts included) lives in this file.  Without
                # it a background+schema run's facts would be unreachable.
                ledger_path=result.ledger_path,
            ))
        except Exception:  # noqa: BLE001 — observation must not break settlement
            _log.debug("background announce failed for %s", run_id, exc_info=True)

    def _reap(self, run_id: str, _task: asyncio.Task[None] | None = None) -> None:
        """Settle a row whose task died without going through ``_finish``.

        A task cancelled before its first step never runs the supervised body
        at all — without this, ``cancel()`` right after spawn would leave the
        row ``running`` forever.  Runs exactly once per task (done-callback);
        a no-op unless the row is still live.
        """
        row = self._background.get(run_id)
        self._tasks.pop(run_id, None)
        announce = self._announce.pop(run_id, None)
        if row is None or row.status != "running":
            return
        self._background[run_id] = replace(
            row, status="cancelled", ended=time.monotonic(),
            error="cancelled before start",
        )
        if announce is None:
            return
        emit, origin_session, name = announce
        if emit is None:
            return
        try:
            emit(BackgroundTaskDone(
                run_id=run_id, name=name, status="cancelled",
                summary="cancelled before start", truncated=True,
                origin_session=origin_session,
            ))
        except Exception:  # noqa: BLE001 — observation must not break settlement
            _log.debug("background reap announce failed for %s", run_id, exc_info=True)

    # ---- internals --------------------------------------------------------

    def _scoped_observer(self, name: str, parent_span: str, inner: Any) -> Any:
        """Wrap an observer so the child's events carry their parentage."""
        if inner is None:
            return None
        return ScopedObserver(
            scope=f"subagent:{name}:{parent_span or 'root'}",
            parent=parent_span or "subagent",
            inner=inner,
        )

    def _skill_gate(self) -> Any | None:
        """The operator's skill policy for a child that cannot prompt (E-5).

        Only the skill rule travels — a child's MCP surface is not this
        task's to change. ``ask`` degrades the way every WARN does for a
        child (``AutoApproveCallback``); ``deny`` still blocks.
        """
        from ai_coding.security.permission_gate import PermissionGate

        raw = getattr(getattr(self._app_config, "ai", None), "skill_permissions", None) or {}
        if not raw:
            return None
        skills = self._skills

        def resolve(name: str) -> str | None:
            skill = skills.resolve(name) if skills is not None else None
            return skill.id if skill is not None else None

        return PermissionGate(skill_permissions=raw, skill_resolver=resolve)

    async def _run_in(
        self,
        task: str,
        name: str,
        workdir: str | None,
        parent: ParentContext | None,
        *,
        parent_span: str = "",
        observer: Any | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> SubagentResult:
        registry = self._build_tools(workdir)
        engine = self._engine_factory()
        ts = now_utc()
        # R7e-4: which window this child gets.  ``task_only`` is a clean context -
        # the parent has to put every fact it needs into the task text - and an
        # empty base string must stay None, because a blank PromptAssembler is a
        # different request, not an equivalent one (an existing test asserts it).
        mode = self.context_mode
        if mode == "task_only":
            seeded: list[ChatMessage] = []
            prompt = (
                PromptAssembler(self._base_instructions)
                if self._base_instructions
                else None
            )
        else:
            seeded = list(parent.messages) if parent is not None else []
            prompt = (
                PromptAssembler(parent.instructions)
                if parent is not None and parent.instructions
                else None
            )
        session = SessionData(
            session_id=f"sub-{uuid.uuid4().hex[:12]}",
            title=name,
            created_time=ts,
            last_access_time=ts,
            messages=seeded,
        )
        loop = AgentLoop(
            engine,
            _NullSessions(),  # type: ignore[arg-type]
            registry,
            approval=self._approval,
            gate=self._skill_gate(),
            prompt=prompt,
            # The same registry the parent uses, so the child reads live skills
            # rather than the frozen copy in the parent's instructions. It used
            # to take none at all, which left the child holding the `skill` tool
            # (the tool registry always got the path) with no catalog naming
            # anything it could load.
            skills=self._skills,
            settings=self._subagent_settings(),
            guards=self._guards,
            workspace=workdir or self._workspace,
            observer=self._scoped_observer(name, parent_span, observer),
        )
        try:
            # The inner loop has its own (slightly tighter) turn deadline, so it
            # normally finalises itself and reports a status.  This outer call is
            # a backstop for a loop that is stuck inside something that ignores
            # cancellation — ``asyncio.wait_for`` could not bound that.
            # R1-3: the format contract rides as a trailer on the task text —
            # the task *is* the child's user message, so no loop/engine change
            # is needed for the child to see it.
            child_task = task
            if output_schema:
                child_task = task + _format_instruction(output_schema)
            # F4: append uncertainty protocol
            child_task += _UNCERTAINTY_PROTOCOL
            await run_with_deadline(
                loop.process_input(session, child_task),
                timeout=self._timeout,
                label="subagent_turn",
            )
        except DeadlineExpired:
            return SubagentResult(
                status="timeout",
                error=f"exceeded {self._timeout:g}s",
                workdir=workdir,
                # R0-3: an outer cut is still a cut — unfinished, with a cause.
                truncated=True,
                exit_reason="outer_timeout",
            )
        finally:
            try:
                await loop.aclose()
            except Exception:  # noqa: BLE001 — releasing resources must not mask the result
                pass
        return self._packet(loop, session, workdir, mode, output_schema)

    def _subagent_settings(self) -> AIConfig:
        """Settings for the sub-turn: its own wall-clock ceiling (R2a)."""
        configured = float(getattr(self._settings, "turn_timeout_seconds", 0.0) or 0.0)
        budget = self._timeout * _INNER_BUDGET_RATIO
        if configured > 0:
            budget = min(budget, configured)
        # C3 off: the parent is blocked on this child, so a continuation buys
        # the parent nothing but latency — and it multiplies the child's
        # iterations by (1 + max_auto_continuations).  Measured with an open
        # todo item and max_tool_iterations=3: 12 model calls instead of 4.
        return self._settings.model_copy(
            update={
                "turn_timeout_seconds": max(budget, 0.01),
                "max_auto_continuations": 0,
            }
        )

    def _packet(
        self, loop: AgentLoop, session: SessionData, workdir: str | None, mode: str = "full",
        output_schema: dict[str, Any] | None = None,
    ) -> SubagentResult:
        outcome = loop.last_outcome
        status = _STATUS_MAP.get(outcome.status.value if outcome else "error", "error")
        limit = int(getattr(self._settings, "max_subagent_conclusion_chars", 4000))
        # R6a: a sub-agent that ran out of budget still has something to say — the
        # harness handoff — and the parent should not have to guess why it stopped.
        conclusion = _truncate(
            (outcome.text or outcome.handoff) if outcome else None, limit
        )
        todos = (
            tuple(item.to_dict() for item in session.task.items)
            if session.task is not None
            else ()
        )
        is_error = status in {"error", "timeout"}
        truncated = status != "completed"
        exit_reason = None
        if outcome is not None and status != "completed":
            exit_reason = outcome.status.value
            if outcome.reason:
                exit_reason = f"{exit_reason}: {outcome.reason}"
        # R1-3: parse what the parent will actually read — the truncated
        # conclusion — so facts can never describe text the parent never saw.
        facts: dict[str, Any] | None = None
        schema_valid: bool | None = None
        schema_errors: tuple[str, ...] = ()
        if output_schema:
            facts, schema_valid, schema_errors = extract_json_facts(
                conclusion, output_schema
            )
        # F4: extract notes and confidence from conclusion
        notes, confidence = _extract_notes_and_confidence(conclusion)
        result = SubagentResult(
            status=status,
            conclusion=conclusion,
            todos=todos,
            artifacts=tuple(loop.last_artifacts),
            iterations=outcome.iterations if outcome else 0,
            workdir=workdir,
            error=(outcome.reason if outcome and outcome.reason else None) if is_error else None,
            context_mode=mode,
            truncated=truncated,
            exit_reason=exit_reason,
            usage=dict(outcome.usage) if outcome is not None and outcome.usage else None,
            facts=facts,
            schema_valid=schema_valid,
            schema_errors=schema_errors,
            notes=notes,
            confidence=confidence,
        )
        # F4: write trace note (sync path — background path does it in _finish_background)
        _write_trace_note(result, self._ledger_root)
        return _with_ledger(result, self._ledger_root)


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _extract_notes_and_confidence(conclusion: str | None) -> tuple[str, str]:
    """Extract notes and confidence from a sub-agent's conclusion.

    Looks for the uncertainty protocol markers in the text.
    Handles markdown formatting (bold, etc.) and both Chinese/English colons.
    Returns (notes, confidence).
    """
    if not conclusion:
        return "", "high"
    notes = ""
    confidence = "high"
    for line in conclusion.split("\n"):
        # Strip markdown formatting
        stripped = line.strip().strip("*").strip()
        # Handle both Chinese and English colons
        for prefix in ["置信度:", "置信度："]:
            if stripped.startswith(prefix):
                value = stripped[len(prefix):].strip().lower()
                if value in ("high", "medium", "low"):
                    confidence = value
                break
        for prefix in ["给后来者的笔记:", "给后来者的笔记："]:
            if stripped.startswith(prefix):
                notes = stripped[len(prefix):].strip()
                break
    return notes, confidence


def _with_ledger(result: SubagentResult, root: Path | None) -> SubagentResult:
    """Write the full packet (untruncated) and return it with the path attached."""
    if root is None:
        return result
    payload = result.to_dict()
    try:
        target_dir = root / "transcripts" / "subagents"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{uuid.uuid4().hex[:12]}.json"
        path.write_text(to_json_pretty(payload), encoding="utf-8")
    except OSError:
        return result  # best-effort: no ledger, but the packet still flows
    return SubagentResult(
        status=result.status,
        conclusion=result.conclusion,
        todos=result.todos,
        artifacts=result.artifacts,
        iterations=result.iterations,
        workdir=result.workdir,
        ledger_path=str(path),
        error=result.error,
        context_mode=result.context_mode,
        truncated=result.truncated,
        exit_reason=result.exit_reason,
        usage=result.usage,
        facts=result.facts,
        schema_valid=result.schema_valid,
        schema_errors=result.schema_errors,
        notes=result.notes,
        confidence=result.confidence,
    )



def _format_instruction(schema: dict[str, Any]) -> str:
    """The machine contract trailer appended to the child's task text (R1-3)."""
    required = schema.get("required", []) if isinstance(schema, dict) else []
    keys = ", ".join(map(str, required)) if isinstance(required, list) else ""
    lines = [
        "",
        "",
        "[response format — machine contract, not prose]",
        "Return your conclusion as a JSON object"
        + (f" with these required keys: {keys}." if keys else "."),
        "Output the JSON object and nothing else (no code fences, no commentary).",
        "If you cannot produce valid JSON, still write your conclusion as plain "
        "text — partial work is never discarded.",
    ]
    return "\n".join(lines)


__all__ = ["SubAgentRunner"]

MAX_TRACE_ENTRIES = 100  #: Cap trace-field.md entries to prevent unbounded growth.


def _write_trace_note(result: SubagentResult, root: Path | None) -> None:
    """Write a note to the shared trace field for future sub-agents.

    Bounded: keeps the last MAX_TRACE_ENTRIES entries (FIFO eviction).
    """
    if root is None or not result.notes:
        return
    try:
        target_dir = root / "transcripts" / "skill-traces"
        target_dir.mkdir(parents=True, exist_ok=True)
        trace_file = target_dir / "trace-field.md"
        with open(trace_file, "a", encoding="utf-8") as f:
            f.write(f"\n---\n[{result.status}] {result.ledger_path}\n{result.notes}\n")
        # Enforce bound: read all lines, keep last N entries
        _enforce_trace_limit(trace_file)
    except OSError:
        pass  # best-effort


def _enforce_trace_limit(trace_file: Path) -> None:
    """Keep only the last MAX_TRACE_ENTRIES entries in the trace file."""
    if not trace_file.exists():
        return
    content = trace_file.read_text(encoding="utf-8")
    entries = content.split("\n---\n")
    # Remove empty first element if file starts with ---
    entries = [e for e in entries if e.strip()]
    if len(entries) > MAX_TRACE_ENTRIES:
        # Keep the last N entries
        kept = entries[-MAX_TRACE_ENTRIES:]
        trace_file.write_text("\n---\n".join(kept) + "\n", encoding="utf-8")


_UNCERTAINTY_PROTOCOL = (
    "\n\n## 不确定协议\n"
    "如果你对结论不完全确定，请在最后用以下格式标记：\n"
    "置信度: [high|medium|low]\n"
    "原因: [为什么不确定]\n"
    "需要: [需要什么信息才能确定]\n"
    "给后来者的笔记: [你踩的坑、发现的捷径、未完成的探索]\n"
)
