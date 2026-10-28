"""Base tool contract.

Every tool exposes:
  - ``name`` / ``description`` / ``params_json_schema`` — for the LLM to discover it
  - ``execute(params) -> ToolResult`` — the actual implementation

The LLM sees the JSON schema; the SDK parses arguments and passes a dict.
Tools return ``ToolResult`` (M0 domain contract) so all callers treat results uniformly.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_coding.domain.message import ChatMessage, ToolResult
from ai_coding.domain.task import TaskState

if TYPE_CHECKING:
    from ai_coding.security.permissions import Rule


def _noop_emit(_event: Any) -> None:
    """Default event sink for contexts built outside the loop."""


@dataclass
class TurnConclusion:
    """Which exit (if any) has closed this turn (N1).

    A plan-mode turn has two exits -- a submitted plan and a research
    conclusion -- and they are mutually exclusive: the first one to succeed
    owns the turn, and the other one is refused.  The holder is shared by
    every tool call of the turn, so the second exit can see that the first
    one already closed it.  It lives in the turn, not in the task: a
    conclusion is a fact about one turn, and the task outlives it.

    ``exit`` is a *success*, nothing else: the plan counts when a human
    approved it, and the research exit counts when the tool returned.  A
    dismissed review is not a success (the human said "not now", so nothing
    was submitted and nothing is armed) -- it only closes the review
    itself, which is why it has its own field: the other exit stays open.
    """

    exit: str = ""
    #: The human dismissed the plan review.  The review is not asked twice in
    #: one turn, but the turn's other exit is still open.
    dismissed: bool = False


@dataclass
class ToolContext:
    """Per-invocation context handed to ``BaseTool.execute``.

    Tools stay stateless singletons (the tool stack is built once and reused
    across sessions); everything session-scoped travels through here.  This is
    also how ``subAgent`` gets the parent turn's context (docs/r1-analysis.md D10).
    """

    task: TaskState = field(default_factory=TaskState)
    workspace: str = "."
    session_id: str = ""
    #: W2/R02: the directories the operator declared in
    #: ``tools.extra_workspace_roots``, already expanded by the composition
    #: root.  A tool that judges whether a path is reachable needs them: a
    #: directory the operator authorized is reachable, and reporting it as
    #: unreachable tells the model to switch workspaces for a path that
    #: already works.
    extra_workspace_roots: tuple[str, ...] = ()
    #: THIS request's view of the history -- already rewritten by compaction, so
    #: it is by construction the part the model can see.  Kept as-is for the
    #: tools that need it (memory_search falls back to the last user line);
    #: anything that must read what compaction dropped needs
    #: :attr:`session_messages` instead.
    messages: tuple[ChatMessage, ...] = ()
    #: The session's own transcript, uncompacted.  ``Compactor`` works on a copy
    #: (``context_compression.py``: "never mutate the caller's history"), so the
    #: bytes a summary dropped are still here -- and before C-1 nothing could
    #: read them (dsh 08 §8-2 and oc 06 §10 both declare the same gap).
    session_messages: tuple[ChatMessage, ...] = ()
    instructions: str | None = None
    #: 18 §10.1: the session's enforcement switch; the dispatcher's gate
    #: refuses mutating tools while this is True.  The gate keeps its single
    #: ``readonly`` input -- this field is not an enforcement source.
    readonly: bool = False
    #: R03: the posture fact for the one tool whose semantics depend on it
    #: (``present_plan`` is plan-only); ``tool_context`` reads it off the
    #: session.  Not the gate's input: enforcement still reads only
    #: ``readonly``.
    mode: str = "normal"
    #: M1a: this session's own rule layer (L4).  It rides the context rather
    #: than the process-wide gate so it is current on every request and
    #: cannot decide another session's calls.  ``dispatch`` passes it to the
    #: gate; an empty tuple means "no session rules".
    permissions: tuple[Rule, ...] = ()
    #: M1b: the current agent's rule layer.  Same reason it rides the
    #: context: the agent can change between requests, and one session's
    #: agent must not decide another session's calls.  The gate treats this
    #: layer's deny as absolute.
    agent_rules: tuple[Rule, ...] = ()
    #: M4/L2: this one call carried human approval for its explicit
    #: credential target.  False is the safe default for direct calls,
    #: automatic approvals, and sibling calls in the same batch.
    credential_approved: bool = False
    emit: Callable[[Any], None] = _noop_emit
    # R4a: the id of the call this execution belongs to.  The dispatcher stamps
    # one context per call (the batch shares everything else), which is what lets
    # a trace hang a sub-agent's spans under the subAgent call that spawned it
    # (docs/r4-analysis.md 4.6).
    call_id: str = ""
    # R7e-1: the session's turn counter, so a note the model writes can record where
    # it came from (`source_turn` means turn, R6d).
    turn: int = 0
    # R8a: how a tool tells the running turn "remember this line for later"
    # (``checkpoint_write``).  ``None`` outside a loop that owns a turn book.
    note_checkpoint: Callable[[str], None] | None = None
    # R20/RC-2: how ``read`` tells the running turn which lines it returned, so
    # the harness can render the ``<read-ledger>`` block from session state
    # instead of trusting a narrative.  Same shape as ``note_checkpoint``: the
    # loop gives it, the tool uses it, ``None`` outside a loop that owns a turn.
    note_read: Callable[[str, int, int, int], None] | None = None
    # P3 (补充式方案 06): how the dispatcher tells the running turn that an
    # approval question is about to be asked (or has been answered).  The loop
    # writes a small marker beside the session file so a crash during the wait
    # does not lose the pending call -- the one place the snapshot was wrong.
    # ``None`` outside a loop that owns a session.
    note_approval: Callable[[dict[str, Any] | None], None] | None = None
    # R10: how a tool tells the running turn that a new plan episode started
    # (``present_plan`` approval).  The loop clears the session's ladder
    # windows through it; ``None`` outside a loop that owns a session.
    note_plan_approved: Callable[[], None] | None = None
    #: N1: the turn's conclusion holder, shared by the turn's tool calls.
    #: The two exits of a plan-mode turn read it to stay mutually exclusive;
    #: ``None`` outside a loop that owns a turn.
    conclusion: TurnConclusion | None = None
    # R8c: a thread-safe "stop" flag for tools that own a subprocess (bash hands
    # it to the process runner).  The dispatcher sets it when the user cancels, so
    # the child dies even though the awaiting task was abandoned.
    kill_event: threading.Event | None = None
    # D-1: the workspace's background-job table, for `bash
    # run_in_background`, `job_list` and `job_kill`.  `None` in a stack built
    # without one (the bare CLI, a sub-agent), where a tool that needs jobs says
    # so rather than answering "nothing is running".  Typed `Any` like
    # `emit`/`notice_checkpoint`: the registry lives in ``core`` and the tools
    # package importing it would drag the loop's world in behind it.
    jobs: Any = None
    # M-C1: the turn's write-ahead log, so a regenerated side-effecting call can
    # be answered from the record instead of running twice.  `None` outside a
    # loop that owns a turn.  Typed `Any` like ``jobs`` for the same reason.
    write_ahead: Any = None


def resolve_ctx(ctx: ToolContext | None) -> ToolContext:
    """Return ``ctx`` or a throwaway default.

    Migration aid: during R1a tools may still be called without a context (legacy
    call sites and unit tests).  From R1a-3 on, ``ToolDispatcher`` always passes a
    real one.
    """
    return ctx if ctx is not None else ToolContext()


class BaseTool(ABC):
    """Abstract base for all tools (built-in and MCP)."""

    # Conservative default: NOT safe to run concurrently with sibling calls in
    # the same assistant message.  Read-only tools opt in by setting True.
    parallel_safe: bool = False
    #: 17 §4-P1: may this tool change the world (workspace, processes, remote
    #: state)?  Plan mode refuses every ``mutates_world`` tool structurally.
    #: Fail-closed default like ``parallel_safe``'s False; the two are
    #: orthogonal — ``subAgent`` runs arbitrary work yet is parallel-safe.
    mutates_world: bool = True
    #: N1: does this tool belong to the planning posture only?  A plan-only
    #: tool is not advertised outside it: the model must not see a tool whose
    #: every call could only be refused (the research exit is plan-only, and
    #: build mode has nothing for it to conclude).  The general version of
    #: this -- a tool table pruned by a permission rule -- is M1a's job; this
    #: is the one hard case this batch needed.
    plan_only: bool = False

    @property
    def origin(self) -> str:
        """Where this tool came from: ``"local"``, or an MCP server's name (R7e-3).

        The tool surface is paid for on every model step, so a cost warning has to
        name the expensive part.  The default keeps every built-in tool out of it.
        """
        return "local"

    def permission_name(self, params: dict[str, Any] | None = None) -> str:
        """The name the *gate* judges, which is not always the registry name.

        One vessel can carry many calls: ``mcp_call`` is authorized as
        ``fs__write_file``, because approving "mcp_call" tells the operator
        nothing about what will run.  Every ordinary tool is its own name.
        """
        return self.name

    @property
    def max_duration_seconds(self) -> float | None:
        """Per-call ceiling for the dispatcher (R2a), in seconds.

        ``None`` means the tool declares no bound of its own, so only the turn
        deadline applies.  Tools that spawn subprocesses or sub-agents override
        this with ``timeout + grace``; the dispatcher takes the tighter of this
        and what is left of the turn.
        """
        return None

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    @property
    @abstractmethod
    def params_json_schema(self) -> dict[str, Any]:
        """JSON Schema (Draft 2020-12 compatible) for the tool parameters."""
        ...

    @abstractmethod
    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        """Execute the tool with already-parsed parameters.

        ``ctx`` carries session-scoped state (task list, workspace, session id,
        parent-turn view).  It is optional only for migration; new call sites
        should always pass one.
        """

    def precheck(self, params: dict[str, Any]) -> ToolResult | None:
        """Cheap refusal for calls that cannot execute — checked before approval.

        The dispatcher runs this *before* asking the user: a call the sandbox
        will reject must not prompt for consent (hotel audit, root cause R2).
        Default ``None`` means "no opinion"; file tools override it with the
        same ``Sandbox`` they execute against, so the two can never disagree.
        """
        return None


__all__ = ["BaseTool", "ToolContext", "resolve_ctx"]
