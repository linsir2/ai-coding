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
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.observers import ScopedObserver
from ai_coding.core.turn_report import report_degraded
from ai_coding.core.worktree import WorktreeError
from ai_coding.domain.session import SessionData
from ai_coding.domain.subagent import ParentContext, SubagentResult
from ai_coding.infra.deadline import DeadlineExpired, run_with_deadline
from ai_coding.infra.json_utils import to_json_pretty
from ai_coding.infra.time_utils import now_utc
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


class _NullSessions:
    """Swallows saves: a sub-agent's transcript is its ledger, not a session file."""

    def save(self, session: SessionData) -> None:
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
    ) -> None:
        self._app_config = app_config
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
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))

    @property
    def timeout_seconds(self) -> float:
        """The budget the tool layer advertises as this runner's per-call ceiling."""
        return self._timeout

    async def run(
        self,
        task: str,
        name: str = "sub-agent",
        *,
        worktree_manager: Any = None,
        isolate: bool = True,
        parent: ParentContext | None = None,
        parent_span: str = "",
    ) -> SubagentResult:
        """Run ``task`` and return its packet.  Never raises for a sub-agent failure."""
        async with self._semaphore:
            if (
                isolate
                and worktree_manager is not None
                and await worktree_manager.can_isolate()
            ):
                try:
                    async with worktree_manager.temporary(name) as wt:
                        return await self._run_in(
                            task, name, str(wt.path), parent, parent_span=parent_span
                        )
                except WorktreeError:
                    report_degraded(
                        self._observer,
                        "worktree_isolate",
                        "the worktree could not be created",
                        "ran in the main workspace instead",
                    )
            return await self._run_in(task, name, None, parent, parent_span=parent_span)

    # ---- internals --------------------------------------------------------

    def _scoped_observer(self, name: str, parent_span: str) -> Any:
        """Wrap the parent's observer so the child's events carry their parentage."""
        if self._observer is None:
            return None
        return ScopedObserver(
            scope=f"subagent:{name}:{parent_span or 'root'}",
            parent=parent_span or "subagent",
            inner=self._observer,
        )

    async def _run_in(
        self,
        task: str,
        name: str,
        workdir: str | None,
        parent: ParentContext | None,
        *,
        parent_span: str = "",
    ) -> SubagentResult:
        registry = self._build_tools(workdir)
        engine = self._engine_factory()
        ts = now_utc()
        session = SessionData(
            session_id=f"sub-{uuid.uuid4().hex[:12]}",
            title=name,
            created_time=ts,
            last_access_time=ts,
            messages=list(parent.messages) if parent is not None else [],
        )
        prompt = (
            PromptAssembler(parent.instructions)
            if parent is not None and parent.instructions
            else None
        )
        loop = AgentLoop(
            engine,
            _NullSessions(),  # type: ignore[arg-type]
            registry,
            approval=self._approval,
            prompt=prompt,
            settings=self._subagent_settings(),
            workspace=workdir or self._workspace,
            observer=self._scoped_observer(name, parent_span),
        )
        try:
            # The inner loop has its own (slightly tighter) turn deadline, so it
            # normally finalises itself and reports a status.  This outer call is
            # a backstop for a loop that is stuck inside something that ignores
            # cancellation — ``asyncio.wait_for`` could not bound that.
            await run_with_deadline(
                loop.process_input(session, task),
                timeout=self._timeout,
                label="subagent_turn",
            )
        except DeadlineExpired:
            return SubagentResult(
                status="timeout",
                error=f"exceeded {self._timeout:g}s",
                workdir=workdir,
            )
        finally:
            try:
                await loop.aclose()
            except Exception:  # noqa: BLE001 — releasing resources must not mask the result
                pass
        return self._packet(loop, session, workdir)

    def _subagent_settings(self) -> AIConfig:
        """Settings for the sub-turn: its own wall-clock ceiling (R2a)."""
        configured = float(getattr(self._settings, "turn_timeout_seconds", 0.0) or 0.0)
        budget = self._timeout * _INNER_BUDGET_RATIO
        if configured > 0:
            budget = min(budget, configured)
        return self._settings.model_copy(
            update={"turn_timeout_seconds": max(budget, 0.01)}
        )

    def _packet(
        self, loop: AgentLoop, session: SessionData, workdir: str | None
    ) -> SubagentResult:
        outcome = loop.last_outcome
        status = _STATUS_MAP.get(outcome.status.value if outcome else "error", "error")
        limit = int(getattr(self._settings, "max_subagent_conclusion_chars", 4000))
        conclusion = _truncate(outcome.text if outcome else None, limit)
        todos = (
            tuple(item.to_dict() for item in session.task.items)
            if session.task is not None
            else ()
        )
        is_error = status in {"error", "timeout"}
        result = SubagentResult(
            status=status,
            conclusion=conclusion,
            todos=todos,
            artifacts=tuple(loop.last_artifacts),
            iterations=outcome.iterations if outcome else 0,
            workdir=workdir,
            error=(outcome.reason if outcome and outcome.reason else None) if is_error else None,
        )
        return _with_ledger(result, self._ledger_root)


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


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
    )


__all__ = ["SubAgentRunner"]
