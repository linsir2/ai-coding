"""Session persistence contract, compatible with the Java ``sessions/*.json`` shape."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .acceptance import AcceptanceItem
from .message import ChatMessage, ImageRef, ToolArtifact, ToolCallRef, tool_call_from_ref
from .plan_delivery import StepDelivery
from .task import TaskState
from .turn_digest import TurnDigest

#: 17 §4-P0: the fail-safe vocabulary for the persisted ``mode`` key — anything
#: unknown reads back as "normal" rather than re-entering as an unknown posture.
KNOWN_MODES: tuple[str, ...] = ("normal", "plan")


def _message_from_dict(data: dict[str, Any]) -> ChatMessage:
    """Rebuild a ChatMessage from a persisted JSON dict (Java-compatible shape)."""
    tool_calls = None
    raw_calls = data.get("toolCalls")
    if isinstance(raw_calls, list):
        tool_calls = [
            ToolCallRef(
                id=item["id"],
                name=item["name"],
                arguments=item.get("arguments", ""),
            )
            for item in raw_calls
            if isinstance(item, dict) and item.get("id") and item.get("name")
        ]
    raw_images = data.get("images")
    images = tuple(
        ImageRef(
            path=str(item.get("path", "")),
            mime=str(item.get("mime", "")),
            name=str(item.get("name", "")),
            bytes=int(item.get("bytes", 0) or 0),
        )
        for item in (raw_images if isinstance(raw_images, list) else [])
        if isinstance(item, dict) and item.get("path")
    )
    return ChatMessage(
        role=data["role"],
        content=data.get("content"),
        timestamp=data.get("timestamp", ""),
        tool_call_id=data.get("toolCallId"),
        tool_name=data.get("toolName"),
        tool_calls=tool_calls,
        reasoning=data.get("reasoning"),
        synthetic=bool(data.get("synthetic", False)),
        artifacts=tuple(
            ToolArtifact.from_dict(item)
            for item in _artifact_rows(data.get("artifacts"))
            if item.get("path")
        ),
        images=images,
        # P1: an older file has no ``id``; ``ChatMessage.__post_init__`` mints
        # one, which is the backfill.  A stored id is kept verbatim.
        id=str(data.get("id", "")),
    )


def _artifact_rows(value: Any) -> list[dict[str, Any]]:
    """The ``artifacts`` list of a stored message, or none when it has none."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _message_to_dict(msg: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {
        "role": msg.role,
        "content": msg.content,
        "timestamp": msg.timestamp,
    }
    # P1: the stable identity travels with every stored message.  It is always
    # minted, so this key is unconditional — an old file simply gains one on
    # its next save.
    if msg.id:
        data["id"] = msg.id
    if msg.tool_call_id is not None:
        data["toolCallId"] = msg.tool_call_id
    if msg.tool_name is not None:
        data["toolName"] = msg.tool_name
    # r17/T5: written only when present — old files (and the Java reader) never
    # see the key, same rule as `reasoning`.
    if msg.images:
        data["images"] = [
            {
                "path": ref.path,
                "mime": ref.mime,
                "name": ref.name,
                **({"bytes": ref.bytes} if ref.bytes else {}),
            }
            for ref in msg.images
        ]
    if msg.tool_calls:
        data["toolCalls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in msg.tool_calls
        ]
    # R1-4: thinking travels with its step, written only when present — old
    # files (and the Java reader) never see the key.
    if msg.reasoning:
        data["reasoning"] = msg.reasoning
    # r18/F3: same rule — a session written before the flag existed reads back
    # as all-human, which is exactly what it was.
    if msg.synthetic:
        data["synthetic"] = True
    # r18/F4: same rule -- a session written before tools reported their side
    # effects reads back as "this step touched nothing", which is the safe
    # direction for /undo.
    if msg.artifacts:
        data["artifacts"] = [artifact.to_dict() for artifact in msg.artifacts]
    return data


@dataclass(frozen=True)
class TurnUsage:
    """One turn's token spend, for post-hoc forensics (was memory-only).

    ``TurnOutcome.usage`` is filled by the reporter after the session save, so
    a past length-cutoff could never be confirmed from the file.  Rows are
    appended in turn order; turns that spent nothing (or reported nothing)
    leave no row rather than a row of zeros.
    """

    turn: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": int(self.turn),
            "promptTokens": int(self.prompt_tokens),
            "completionTokens": int(self.completion_tokens),
            "totalTokens": int(self.total_tokens),
        }

    @classmethod
    def from_dict(cls, data: Any) -> TurnUsage | None:
        if not isinstance(data, dict):
            return None
        try:
            turn = int(data.get("turn", 0) or 0)
        except (TypeError, ValueError):
            return None
        if turn <= 0:
            return None
        return cls(
            turn=turn,
            prompt_tokens=int(data.get("promptTokens", 0) or 0),
            completion_tokens=int(data.get("completionTokens", 0) or 0),
            total_tokens=int(data.get("totalTokens", 0) or 0),
        )


@dataclass(frozen=True)
class StagedUndo:
    """What ``/undo`` took away, in a form that outlives the process (P4).

    ``removed`` is the message tail it cut.  ``turns``/``turn_count``/``digests``
    are the counter state as it stood before the *first* undo of the run: a
    second undo only widens the staged revert (r20/F2), so re-reading the
    trimmed list would duplicate what survived and adding to the count would
    invent turns that never ran.
    """

    removed: tuple[ChatMessage, ...] = ()
    turns: int = 0
    turn_count: int = 0
    digests: tuple[TurnDigest, ...] = ()


@dataclass
class SessionData:
    """A persisted conversation session."""

    session_id: str
    title: str
    created_time: str
    last_access_time: str
    messages: list[ChatMessage] = field(default_factory=list)
    task: TaskState | None = None
    # R3b: how many messages have already been harvested into memory.  The cursor
    # lives in the session (not in the loop) because the CLI builds a fresh loop
    # for every turn, while the session is reloaded each time.
    harvested_messages: int = 0
    # R6a: the session-level turn counter (1-based) and the bounded ring of
    # one-line turn digests the volatile <working-memory> block is rendered from.
    # The counter exists so `source_turn` and the digest header can say "turn N"
    # instead of "message count" (docs/r6-analysis.md 2.4).
    turn_count: int = 0
    turn_digests: list[TurnDigest] = field(default_factory=list)
    # Per-turn token spend (see TurnUsage).  Sessions written before this
    # existed simply have no key; the file stays readable everywhere.
    turn_usages: list[TurnUsage] = field(default_factory=list)
    # M11: the turn the last compaction happened in. Usage rows at or before it
    # measured a context that no longer exists, so the context meter ignores them
    # (see `server.handlers.context_tokens`). 0 means "never compacted", which
    # keeps the ordinary reading — the newest turn — unchanged.
    last_compacted_turn: int = 0
    # NOT persisted — ``to_dict`` is a hand-written whitelist, so this stays out
    # of the on-disk format and the Java side keeps reading the same bytes.
    # ``SessionService.list_sessions`` reads every file anyway and drops the
    # bodies; without this the only count a list view could show was the zero it
    # just wrote itself.  -1 means "not computed" (a full ``load``), so callers
    # must treat it as "use ``len(messages)``" rather than as zero.
    message_count: int = -1
    # NOT persisted — ``to_dict`` is a hand-written whitelist, so this stays out
    # of the on-disk format.  It is a normalized excerpt from the first user
    # message, used by the resume picker.
    first_preview: str = ""
    # Acceptance contracts (R0-1): machine-checkable statements plus the shell
    # command proving each one.  Populated programmatically for now (the plan
    # runner will produce these); persisted only when non-empty, same rule as
    # ``task`` — old files simply have no key.
    acceptance: list[AcceptanceItem] = field(default_factory=list)
    # C3: continuation rounds this turn has already spent (the iteration budget
    # died with items open, the loop started a fresh round).  Kept on the
    # session because the books are per-turn and would lose it across rounds —
    # in-process only: ``to_dict`` is a whitelist and a new user turn resets it.
    continuations: int = 0
    # M7: how many times this turn has asked a model that thought-and-stopped to
    # finish its answer.  Same lifetime as ``continuations`` — per turn, in
    # process — because the point of it is to cap the retry inside one turn, not
    # to remember anything about the operator.
    thinking_retries: int = 0
    # NOT persisted, by the same whitelist.  Internal sessions (plan-runner
    # steps) never touch the sessions directory: they run on the main loop,
    # whose TurnCloser would otherwise write one "plan step N" file per step
    # and flood the /resume picker.
    ephemeral: bool = False
    # The skill ids last listed in the system prompt. `skill_review` writes new
    # skills every few turns, so the catalog does change mid-session — three
    # places in our docs claim it cannot. Rather than repeat a claim the code
    # does not keep, the change is declared: a prompt only grows, so a removal
    # has to be written as an explicit negation. In-process only.
    listed_skill_ids: tuple[str, ...] = ()
    # M1: in-process only — skill review tracks which turns and messages have
    #: been reviewed.  Not persisted (to_dict is a whitelist); restarting
    #: re-reviews from 0, which is idempotent (won't create duplicate skills).
    skill_review_counter: int = 0
    skill_review_cursor: int = 0
    # Step deliveries (R1-1): what each plan-runner step left behind.  The
    # resume replay reads these to rebuild the step timeline; persisted only
    # when non-empty, same rule as above.
    step_deliveries: list[StepDelivery] = field(default_factory=list)
    # ---- P1 (补充式方案 06): the three facts recovery was missing ----------
    # Each is written only when set (same rule as ``task``), so an ordinary
    # session file gains no keys until one of these situations actually arises.
    #: The turn number whose turn is open right now.  ``None`` between turns; a
    #: non-``None`` value after ``load`` means the process died mid-turn
    #: (scenario ①: crash-resume).
    open_turn: int | None = None
    #: The approval request currently awaiting an answer (scenario ③).  Loaded
    #: from the pending-approval marker beside the session file, never written
    #: to it: persisting it mid-batch would put an unpaired tool call through
    #: ``ensure_tool_pairing``, which drops the carrier and with it the pending
    #: call (verified).  The marker is written by the loop and read on load.
    pending_approval: dict[str, Any] | None = None
    #: What ``/undo`` removed, kept so ``/redo`` survives a restart
    #: (scenario ④).  The message half lives here; the bytes of files the turn
    #: created live in the undo sidecar (``core/undo_store.py``).
    undo_state: dict[str, Any] | None = None
    #: 13 §2.1: the intervention ladder ledger ({gate: {level, at, outcome,
    #: fires, ref}}).  Written only when non-empty; cleared by a new plan and
    #: by task completion — the ladder is per task, not per session.
    intervention_state: dict[str, Any] | None = None
    #: 17 §4-P0: the cognition posture ("normal" | "plan").  Written only when
    #: not normal — same rule as ``task``; unknown values read back as normal.
    mode: str = "normal"
    #: 18 §10.1: the enforcement switch.  A read-only session refuses every
    #: ``mutates_world`` tool at the dispatch gate; written only when True
    #: (same rule again).  Orthogonal to ``mode`` on purpose — ``/plan``
    #: composes the two at the command layer.
    readonly: bool = False
    #: 18 §10.2: a posture flip requested while a turn is running waits here
    #: until that turn closes (``AgentLoop._apply_pending_posture``) instead of
    #: changing ``mode``/``readonly`` under a live turn.  Runtime only — a
    #: crash drops a still-queued flip (the in-process limit dsh documents);
    #: shape ``{"mode": str | None, "readonly": bool | None}``, later wins.
    pending_posture: dict[str, Any] | None = None
    #: 13 §2.4 (F): the workspace's git branch, refreshed once per turn by the
    #: loop (``infra.vcs.git_branch`` never raises).  Runtime only — the
    #: whitelist ``to_dict`` deliberately leaves it out of the file.
    git_branch: str = ""
    # M3: in-process only — tool call observations for SOP routing.
    # Recorded by ToolObserver, consumed by SOPRouter for failure_pattern matching.
    # Not persisted (to_dict is a whitelist); keeps last 50 entries.
    tool_observations: list[Any] = field(default_factory=list)
    # M4: in-process only — skill recommendation/loading stats for freshness scoring.
    # Tracked by SOPRouter (recommendations) and SkillTool (loads).
    # Not persisted (to_dict is a whitelist).
    skill_stats: dict[str, Any] = field(default_factory=dict)
    # F1: in-process only — cross-turn error → recovery mapping.
    # Key = sha1(error)[:12], Value = {"recovery": str, "used_count": int, "last_used_turn": int}
    # Not persisted (to_dict is a whitelist).
    error_recovery_cache: dict[str, Any] = field(default_factory=dict)
    # I2: in-process only — constraint effectiveness tracking.
    # Key = constraint text, Value = {"injected": bool, "followed": bool, "violation_count": int}
    # Not persisted (to_dict is a whitelist).
    constraint_tracking: dict[str, Any] = field(default_factory=dict)
    # I3: in-process only — pre-compaction task anchor (goal + key constraints).
    # Captured before compaction, injected after if drift detected.
    # Not persisted (to_dict is a whitelist).
    pre_compaction_anchor: str = ""
    # F1: in-process only — skill co-activation graph.
    # Key = from_skill, Value = {to_skill: count}
    # Not persisted (to_dict is a whitelist).
    coactivation_graph: dict[str, dict[str, int]] = field(default_factory=dict)
    # F1: in-process only — name of skill loaded this turn (for outcome tracking).
    # Not persisted (to_dict is a whitelist).
    loaded_skill: str = ""
    # P1: NOT persisted (to_dict is a whitelist).  True when ``load`` found an
    # open turn, so the server can tell the user "the last turn did not finish"
    # once; the next turn clears ``open_turn`` and with it this notice.
    recovered_interrupted: bool = False

    def begin_plan(self, goal: str) -> TaskState:
        """Start a new plan: fresh goal, no items, no step receipts (R1-5b).

        The three belong together.  Receipts are keyed by step id and ids
        restart at 1 for every new list, so carrying the old plan's receipts
        over made the runner's backfill believe the new plan's steps were
        already receipted (measured: a reset plan produced zero receipts).
        One owner for the trio, so ``/plan`` cannot reset the goal and
        forget the book again.
        """
        self.task = TaskState(goal=goal)
        self.step_deliveries = []
        # 13 §2.1 (Q2 拍板): a new plan starts on a clean ladder.
        self.intervention_state = None
        return self.task

    def add_message(self, message: ChatMessage) -> None:
        self.messages.append(message)

    def stage_undo(
        self,
        removed: Sequence[ChatMessage],
        *,
        turns: int,
        turn_count: int,
        digests: Sequence[TurnDigest],
    ) -> None:
        """Record the staged revert on the session so ``/redo`` outlives the
        process (P4).

        The message and digest payloads use the session file's own
        (de)serialization, so a reload rebuilds identical values rather than a
        lookalike; the loop keeps its process-local copies only as a cache.
        """
        self.undo_state = {
            "removed": [_message_to_dict(m) for m in removed],
            "turns": int(turns),
            "turnCount": int(turn_count),
            "digests": [d.to_dict() for d in digests],
        }

    def staged_undo(self) -> StagedUndo | None:
        """Read the staged revert back, or ``None`` when nothing is staged."""
        state = self.undo_state
        if not isinstance(state, dict):
            return None
        rows = state.get("removed")
        removed = tuple(
            _message_from_dict(row)
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
        )
        digest_rows = state.get("digests")
        digests = tuple(
            digest
            for digest in (
                TurnDigest.from_dict(row)
                for row in (digest_rows if isinstance(digest_rows, list) else [])
                if isinstance(row, dict)
            )
            if digest is not None
        )
        return StagedUndo(
            removed=removed,
            turns=int(state.get("turns", 0) or 0),
            turn_count=int(state.get("turnCount", 0) or 0),
            digests=digests,
        )

    def live_prompt_tokens(self) -> int:
        """The newest turn's prompt tokens that still describe the live context.

        Two readers need this and they once answered it differently: the sidebar
        meter skipped usage at or before the compaction boundary, while the
        compaction gate read the last row outright. The gate therefore kept
        seeing 237,157 through a boundary that had already invalidated it and
        compacted again, immediately, before the model had produced anything to
        measure — each time paying for a summary it then discarded.

        The boundary is session state, so the rule belongs to the session. One
        implementation, two callers, no way to drift again.
        """
        if not self.turn_usages:
            return 0
        live = [row for row in self.turn_usages if int(row.turn) > self.last_compacted_turn]
        if not live:
            return 0
        return int(live[-1].prompt_tokens or 0)

    def record_turn_usage(self, turn: int, usage: dict[str, int] | None) -> None:
        """Append one usage row; absent or all-zero usage writes no row."""
        if not usage or not any(int(value or 0) for value in usage.values()):
            return
        self.turn_usages.append(
            TurnUsage(
                turn=int(turn),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
            )
        )

    def ensure_tool_pairing(self, strict: bool = True) -> None:
        """Drop orphan tool results and strip unmatched tool calls (defensive)."""
        declared = {
            c.id
            for m in self.messages
            if m.role == "assistant" and m.tool_calls
            for c in m.tool_calls
        }
        result_ids = {
            m.tool_call_id
            for m in self.messages
            if m.role == "tool" and m.tool_call_id in declared
        }
        keep: list[ChatMessage] = []
        for m in self.messages:
            if m.role == "tool":
                if m.tool_call_id in declared:
                    keep.append(m)
                continue
            if strict and m.role == "assistant" and m.tool_calls and not m.content:
                # Only strip orphan calls from "carrier" messages (no text content).
                # For messages with text, keep all tool_calls as historical record.
                kept = [c for c in m.tool_calls if c.id in result_ids]
                if not kept:
                    continue  # assistant carrier message with no surviving calls
                m.tool_calls = kept
            keep.append(m)
        self.messages = keep

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sessionId": self.session_id,
            "title": self.title,
            "createdTime": self.created_time,
            "lastAccessTime": self.last_access_time,
            "messages": [_message_to_dict(m) for m in self.messages],
        }
        # Only persist the task list when it actually holds items: a bare goal is
        # already recoverable from the first user message, so writing it would be
        # noise in every session file.
        if self.task is not None and self.task.items:
            data["task"] = self.task.to_dict()
        if self.harvested_messages:
            data["harvestedMessages"] = self.harvested_messages
        # Same rule as `task`: an empty ring is not worth a key in every file.
        if self.turn_count:
            data["turnCount"] = int(self.turn_count)
        if self.turn_digests:
            data["turnDigests"] = [digest.to_dict() for digest in self.turn_digests]
        # Same rule again: zero-spend turns leave no rows, so most test and
        # idle sessions never grow this key at all.
        if self.turn_usages:
            data["turnUsages"] = [row.to_dict() for row in self.turn_usages]
        # Same rule again.  Without this the boundary survives only in memory:
        # reloading a compacted session brought back the usage rows the
        # compaction had already invalidated, and the meter read them again.
        if self.last_compacted_turn:
            data["lastCompactedTurn"] = int(self.last_compacted_turn)
        # Same rule again: sessions without acceptance contracts write no key,
        # so every file written before R0-1 keeps reading byte-identically.
        if self.acceptance:
            data["acceptance"] = [item.to_dict() for item in self.acceptance]
        # Same rule again: sessions never plan-run write no key.
        if self.step_deliveries:
            data["stepDeliveries"] = [entry.to_dict() for entry in self.step_deliveries]
        # P1: the three recovery facts, each only when set, same rule again.
        if self.open_turn is not None:
            data["openTurn"] = int(self.open_turn)
        if self.undo_state is not None:
            data["undoState"] = dict(self.undo_state)
        # Same rule again: a session that never hit a ladder gains no key.
        if self.intervention_state:
            data["interventionState"] = dict(self.intervention_state)
        # Same rule again (17 §4-P0): a normal session gains no key either.
        if self.mode and self.mode != "normal":
            data["mode"] = self.mode
        # Same rule again (18 §10.1): a writable session gains no key.
        if self.readonly:
            data["readonly"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionData:
        messages = [
            _message_from_dict(m)
            for m in data.get("messages", [])
            if isinstance(m, dict)
        ]
        raw_task = data.get("task")
        raw_digests = data.get("turnDigests")
        rows = raw_digests if isinstance(raw_digests, list) else []
        digests = [
            digest
            for digest in (TurnDigest.from_dict(row) for row in rows)
            if digest is not None
        ]
        raw_usages = data.get("turnUsages")
        usage_rows = raw_usages if isinstance(raw_usages, list) else []
        usages = [
            row
            for row in (TurnUsage.from_dict(item) for item in usage_rows)
            if row is not None
        ]
        raw_acceptance = data.get("acceptance")
        acceptance_rows = raw_acceptance if isinstance(raw_acceptance, list) else []
        acceptance = [
            AcceptanceItem.from_dict(row) for row in acceptance_rows
            if isinstance(row, dict)
        ]
        raw_deliveries = data.get("stepDeliveries")
        delivery_rows = raw_deliveries if isinstance(raw_deliveries, list) else []
        step_deliveries = [
            StepDelivery.from_dict(row) for row in delivery_rows
            if isinstance(row, dict)
        ]
        raw_open_turn = data.get("openTurn")
        raw_undo = data.get("undoState")
        raw_intervention = data.get("interventionState")
        raw_mode = str(data.get("mode") or "normal")
        return cls(
            session_id=data.get("sessionId", ""),
            title=data.get("title", ""),
            created_time=data.get("createdTime", ""),
            last_access_time=data.get("lastAccessTime", ""),
            messages=messages,
            task=TaskState.from_dict(raw_task) if isinstance(raw_task, dict) else None,
            harvested_messages=int(data.get("harvestedMessages", 0) or 0),
            turn_count=int(data.get("turnCount", 0) or 0),
            turn_digests=digests,
            turn_usages=usages,
            last_compacted_turn=int(data.get("lastCompactedTurn", 0) or 0),
            acceptance=acceptance,
            step_deliveries=step_deliveries,
            open_turn=int(raw_open_turn)
            if isinstance(raw_open_turn, int) and not isinstance(raw_open_turn, bool)
            else None,
            undo_state=dict(raw_undo) if isinstance(raw_undo, dict) else None,
            intervention_state=(
                dict(raw_intervention) if isinstance(raw_intervention, dict) else None
            ),
            mode=raw_mode if raw_mode in KNOWN_MODES else "normal",
            # Truthy garbage coerces to the *restrictive* value: fail-closed.
            readonly=bool(data.get("readonly")),
        )


# Re-export helper for callers that need to resolve references.
__all__ = ["KNOWN_MODES", "SessionData", "StagedUndo", "TurnUsage", "tool_call_from_ref"]
