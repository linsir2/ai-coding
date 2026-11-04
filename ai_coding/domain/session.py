"""Session persistence contract, compatible with the Java ``sessions/*.json`` shape."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .agents import is_plan_agent
from .effects import EffectLedger, FileReceipt
from .message import ChatMessage, ImageRef, ToolArtifact, ToolCallRef, tool_call_from_ref
from .read_ledger import ReadMark, normalize
from .task import TaskState
from .turn_digest import TurnDigest

#: R20: the ceiling on ``seen_paths``.  A session that greps a whole repository
#: names thousands of files; the citation check only needs "was this one ever
#: mentioned", so a long tail is dead weight in every session file.
_SEEN_PATHS_CAP = 200


def _read_mark_from_dict(row: Mapping[str, Any]) -> ReadMark | None:
    """One persisted ledger row; a malformed row is skipped, never fatal."""
    try:
        path = str(row["path"])
        start = int(row["start"])
        end = int(row["end"])
        total = int(row.get("total", 0) or 0)
        version = str(row.get("version", "") or "")
        call_id = str(row.get("callId", row.get("call_id", "")) or "")
    except (KeyError, TypeError, ValueError):
        return None
    if not path:
        return None
    return ReadMark(path, start, end, total, version, call_id)


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
        # M1b T1b: absent means "not attributed", which is what every message
        # written before the agent table existed reads back as.
        agent=str(data.get("agent", "") or ""),
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
    # M1b T1b: written only when present — an old file (and the Java reader)
    # never see the key, same rule as ``reasoning``.
    if msg.agent:
        data["agent"] = msg.agent
    return data


@dataclass
class InboxItem:
    """One queued line of operator input, and where it goes (D-2).

    dsh's inbox has the same two targets: ``next-turn`` runs after this turn,
    ``next-step`` joins it (``agent-loop/src/agent.ts``: ``followup`` /
    ``steer``).  oc's table calls them queue/steer deliveries.  The id is
    stable across edits so the panel can address a row; the text is what the
    operator typed, verbatim.
    """

    id: str
    text: str
    target: str = "next-turn"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "text": str(self.text),
            "target": str(self.target),
            "createdAt": str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: Any) -> InboxItem | None:
        if not isinstance(data, dict):
            return None
        text = data.get("text")
        if not isinstance(data.get("id"), str) or not isinstance(text, str):
            return None
        target = data.get("target", "next-turn")
        if target not in ("next-turn", "next-step"):
            return None
        created = data.get("createdAt", "")
        return cls(
            id=data["id"],
            text=text,
            target=target,
            created_at=created if isinstance(created, str) else "",
        )


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
    #: 19 §3.5: the window's occupancy — the turn's LAST step's prompt tokens,
    #: not the summed spend above.  Older rows carry 0 and the session's rule
    #: falls back to ``prompt_tokens`` for them.
    context_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": int(self.turn),
            "promptTokens": int(self.prompt_tokens),
            "completionTokens": int(self.completion_tokens),
            "totalTokens": int(self.total_tokens),
            "contextTokens": int(self.context_tokens),
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
            context_tokens=int(data.get("contextTokens", 0) or 0),
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


def _plan_body(text: str) -> str | None:
    """The first ``<proposed_plan>`` body in one historical message."""
    start = (text or "").find("<proposed_plan>")
    if start < 0:
        return None
    body_start = start + len("<proposed_plan>")
    end = text.find("</proposed_plan>", body_start)
    body = text[body_start:None if end < 0 else end].strip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    return body


def _approved_plan_from_history(messages: Sequence[ChatMessage]) -> str:
    """Recover the plan that an old session already approved.

    Sessions written before ``approvedPlan`` existed still carry the two
    reliable facts: the ``<proposed_plan>`` block and the synthetic
    ``Implement the plan.`` turn that followed it.  Pair the latter with the
    latest plan proposed before it; later unapproved plans must not overwrite
    an executed one.
    """
    candidate = ""
    executed = ""
    for message in messages:
        content = message.content or ""
        if message.role == "assistant":
            plan = _plan_body(content)
            if plan:
                candidate = plan
        elif (
            message.role == "user"
            and (content or "").strip().startswith("Implement the plan.")
            and candidate
        ):
            executed = candidate
    return executed


@dataclass
class SessionData:
    """A persisted conversation session."""

    session_id: str
    title: str
    created_time: str
    last_access_time: str
    messages: list[ChatMessage] = field(default_factory=list)
    task: TaskState | None = None  # todo owner is the owning agent session; no is_subagent field.
    #: C-3 follow-up: the last plan the human approved.  This is context, not a
    #: goal and not a second task list: ``update_plan`` still owns the task
    #: items.  Persisted so a reload/compaction cannot bury the plan that the
    #: current request may be continuing.
    approved_plan: str = ""
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
    # D-2: the operator's queued input, both targets in one table (dsh's
    # next-turn/next-step, oc's queue/steer).  Lives on the session so a restart
    # keeps what the operator meant to ask next; the loop drains next-step rows
    # into a running turn and the next finished turn picks up next-turn rows.
    inbox: list[InboxItem] = field(default_factory=list)
    # M11: the turn the last compaction happened in. Usage rows at or before it
    # measured a context that no longer exists, so the context meter ignores them
    # (see `server.handlers.context_tokens`). 0 means "never compacted", which
    # keeps the ordinary reading — the newest turn — unchanged.
    last_compacted_turn: int = 0
    # NOT persisted (``to_dict`` is a hand-written whitelist).  The provider's
    # own context reading for the most recent model step *of the running turn*.
    # ``turn_usages`` is only filled when a turn closes, so without this the
    # compaction gate saw a stale (or zeroed) reading for every iteration after
    # the first compaction of a turn and fell back to the character estimate --
    # which measures the un-compacted history and never shrinks.  Measured live
    # 2026-10-30: 21 iterations, 21 compactions.  0 means "no step has reported".
    live_context_tokens: int = 0
    # NOT persisted.  C-2/C-3 (codex path): this turn is the one Codex sends for
    # an approved plan (the literal "Implement the plan." prompt).  In-process
    # only -- ``to_dict`` is a whitelist, so a crash during it resumes as an
    # ordinary turn, which is what Codex does too (it has no cross-process
    # plan-turn recovery).
    plan_implementing: bool = False
    # NOT persisted.  C-0/C-1: the codex path's item-boundary proof
    # switch, mirrored onto the session for the duration of one run.  Same
    # lifetime as ``plan_implementing``.
    plan_verify_items: bool = False
    # Persisted.  Tools the model has asked for (``tool_search``): their schemas
    # are deferred, so this is what makes them appear on the *next* request.
    # Capped by the loop; a tool loaded once stays loaded for the session (the
    # tool surface is unrelated to history, so compaction does not clear it).
    loaded_tools: tuple[str, ...] = ()
    # I2: the checkpoint the context pipeline installed, and how much of
    # ``messages`` it stands for.  Compaction is recomputed from the full history
    # on every request, so without these three the summariser re-read the same
    # opening of the conversation every time and its verdict never advanced
    # (measured: two summaries 21 iterations apart, both "blocked: conversation
    # truncated").  ``compaction_anchor`` is a content fingerprint of
    # ``messages[:consumed]`` (role + body + tool-call arguments), not an id:
    # ``ChatMessage.id`` is empty for every message the loop creates, so
    # comparing ids compared "" == "" and never invalidated anything.  Undo,
    # rewind, an edited turn or any external rewrite changes the fingerprint,
    # and a checkpoint that no longer describes the history in front of it must
    # not be carried forward.
    compaction_checkpoint: str = ""
    compaction_consumed: int = 0
    compaction_anchor: str = ""
    # I2: how many consecutive L4 failures this *session* has had.  The counter
    # used to live on the shared ``ContextManager``, so three failures in one
    # session stopped summarisation for every other session in the process.
    # Memory-only and reset at the start of every turn (``AgentLoop._begin_turn``):
    # the breaker exists to stop hammering a broken summariser within a turn,
    # not to disable summarisation for the life of the session.
    compaction_failures: int = 0
    # R20/RC-2: the read ledger.  Which lines this session has read, and the
    # file sizes that make the gaps computable.  Persisted (ranges only, never
    # content) and rewritten every iteration into the ``<read-ledger>`` tail
    # block -- a volatile block, so compaction cannot reach it.  The checkpoint
    # stores conclusions; this stores evidence.
    effects: EffectLedger = field(default_factory=EffectLedger)
    #: 0 = loaded from a session written before effectsVersion existed, 2 =
    #: written by the EffectLedger-era code.  This is the persisted discriminator
    #: that an empty receipts list alone cannot provide.
    effects_version: int = 2
    #: The operator allowed the first completion override in this session;
    #: later overrides do not ask again.  Persisted so a resume keeps it.
    completion_override_approved: bool = False
    # R20: every source path any tool result named.  Tier 1 of the citation
    # check ("the model cited a file this session never touched") needs it, and
    # it must be *accumulated*, not derived from ``messages``: compaction keeps
    # only a 2,000-character excerpt of each tool result, so a derived set would
    # forget the early ones and reject a legitimate citation.
    seen_paths: tuple[str, ...] = ()
    # R20/RC-7: how many times this turn has sent the model's answer back for
    # citing code it never read.  Per turn, in process (same lifetime as
    # ``compaction_failures``): one correction chance per answer, not one per
    # session, and never carried across turns.
    claim_retries: int = 0
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
    #: M-C1: the turn's in-flight write-ahead records, read back from the
    #: sidecar on ``load`` (``pending_approval``'s shape).  Runtime only -- the
    #: durable copy is ``sessions/write-ahead/<id>.json``, so the session file
    #: and its save cadence do not change.
    write_ahead: list[Any] | None = None
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
    #: M1b T1: the agent this session speaks through — the *only* stored
    #: authority for its posture.  ``mode`` is derived from it (see the
    #: property below), so the file carries one fact about the posture, not
    #: two that have to agree.
    agent: str = "build"
    #: M1b T1b: the durable record of every agent switch — who was in
    #: charge, who took over, when and why.  Only grows; a row is written
    #: when a switch is committed, never edited afterwards.
    agent_history: list[dict[str, Any]] = field(default_factory=list)
    #: 18 §10.1: the enforcement switch.  A read-only session refuses every
    #: ``mutates_world`` tool at the dispatch gate; written only when True
    #: (same rule again).  Orthogonal to ``mode`` on purpose — ``/plan``
    #: composes the two at the command layer.
    readonly: bool = False
    #: 18 §10.2 / M1b T10: a posture flip requested while a turn is running
    #: waits here until that turn closes (``AgentLoop._apply_pending_posture``)
    #: instead of changing ``agent``/``readonly`` under a live turn.  Runtime
    #: only — the durable copy is a sidecar (M1b); shape
    #: ``{"agent": str | None, "readonly": bool | None, "reason": str}``.
    pending_posture: dict[str, Any] | None = None
    #: M2 T3: the plan reminder waiting to be delivered.  A reminder is
    #: queued when the agent changes and turns into a synthetic message at
    #: the turn's boundary -- it cannot be a message at the moment of the
    #: switch, because a turn may be running and its request is already
    #: assembled.  Runtime only, like ``pending_posture``: the durable copy
    #: is a sidecar.
    pending_reminders: list[dict[str, Any]] | None = None
    #: M1a: this session's own permission rules (the L4 layer), in the same
    #: JSON form the rule files use.  It stays on the session -- rather than
    #: in the process-wide gate -- so one session's rule cannot decide
    #: another's calls, and so it is current on the very next request.
    #: Written only when non-empty, like every other optional key.
    permissions: list[dict[str, Any]] = field(default_factory=list)
    #: Reasons a stored session rule could not be read.  Runtime only: the
    #: raw rows stay on the session, and the consumer reports these in
    #: ``/permissions`` rather than dropping the evidence.
    permission_errors: tuple[str, ...] = ()
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

    # ---- the derived posture ------------------------------------------------

    @property
    def mode(self) -> str:
        """The cognition posture, derived from the agent (M1b T1).

        ``"plan"`` when the session speaks through the plan agent, ``"normal"``
        otherwise.  It is a **projection, not a field**: there is no second
        place for the posture to live, so the two cannot drift, and assigning
        to it raises rather than quietly creating one.
        """
        return "plan" if is_plan_agent(self.agent) else "normal"

    def begin_plan(self, goal: str) -> TaskState:
        """Start a new plan: a fresh goal and no items (R1-5b).

        The goal and the item list are one book, so ``/plan`` cannot reset one
        and forget the other.
        """
        self.task = TaskState(goal=goal)
        # A new plan must not inherit the previous approved plan or the previous
        # plan's file receipts.  Sequence is monotonic, so old started_seq
        # values still sort correctly.
        self.approved_plan = ""
        self.effects = self.effects.clear_files()
        # 13 §2.1 (Q2 拍板): a new plan starts on a clean ladder.
        self.intervention_state = None
        return self.task

    def add_message(self, message: ChatMessage) -> None:
        self.messages.append(message)

    # ---- R20/RC-2: the read ledger ---------------------------------------

    @property
    def read_ledger(self) -> tuple[ReadMark, ...]:
        """Merged display/citation view; raw provenance lives in ``effects``."""
        return self.effects.merged_reads()

    @property
    def effects_legacy(self) -> bool:
        return self.effects_version == 0

    def note_read(
        self,
        path: str,
        start: int,
        end: int,
        total: int = 0,
        *,
        version: str = "",
        call_id: str = "",
        canonical: bool = False,
        workspace: str = "",
    ) -> None:
        """Record that lines ``start..end`` of ``path`` reached the context.

        The path is normalised here rather than trusted from the caller: the
        same session receives absolute and relative spellings of one file, and
        two keys for one file would make ``covers`` miss a citation.  The range
        is the *returned* one, so a paged read records its page, not the file.
        """
        key = str(path) if canonical else normalize(path, workspace)
        if not key or end < start:
            return
        self.effects = self.effects.note_read(
            ReadMark(key, start, end, total, version, call_id)
        )

    def record_effect(
        self,
        path: str,
        version: str,
        *,
        tool: str,
        call_id: str = "",
        kind: str = "write",
        confidence: str = "exact",
    ) -> None:
        """Record one file effect and invalidate older reads of that path."""
        key = str(path)
        if not key:
            return
        self.effects = self.effects.invalidate_reads((key,)).record_effect(
            key,
            str(version),
            tool=tool,
            call_id=call_id,
            kind=kind,
            confidence=confidence,
        )

    def invalidate_reads(self, paths: Iterable[str], *, workspace: str = "") -> None:
        """Mark known file evidence stale after a write/edit/merge.

        Emptying ``version`` is deliberate: ``render`` treats an empty version
        as unknown/must-recheck, never as a complete read.  Paths that were
        never read are ignored.
        """
        keys = {normalize(path, workspace) for path in paths}
        keys.discard("")
        if not keys:
            return
        self.effects = self.effects.invalidate_reads(tuple(keys))

    def note_seen(self, paths: Iterable[str]) -> None:
        """Accumulate the source paths a batch of tool results named.

        Deduplicated and capped: the newest are what the model is working from,
        and a long session must not grow this without bound.
        """
        fresh = [p for p in dict.fromkeys(paths) if p and p not in self.seen_paths]
        if fresh:
            self.seen_paths = (self.seen_paths + tuple(fresh))[-_SEEN_PATHS_CAP:]

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
        """The newest measurement of the context that is actually live.

        Two readers need this and they once answered it differently: the sidebar
        meter skipped usage at or before the compaction boundary, while the
        compaction gate read the last row outright. The gate therefore kept
        seeing 237,157 through a boundary that had already invalidated it and
        compacted again, immediately, before the model had produced anything to
        measure — each time paying for a summary it then discarded.

        The boundary is session state, so the rule belongs to the session. One
        implementation, two callers, no way to drift again.

        ``live_context_tokens`` comes first: it is the running turn's own step
        reading, and a turn only writes ``turn_usages`` when it closes, so the
        rows below are always one turn behind. Without the in-turn value every
        iteration after the first compaction read a boundary-invalidated number.
        """
        if self.live_context_tokens > 0:
            return int(self.live_context_tokens)
        if not self.turn_usages:
            return 0
        live = [row for row in self.turn_usages if int(row.turn) > self.last_compacted_turn]
        if not live:
            return 0
        row = live[-1]
        # 19 §3.5: the row's prompt total is the turn's *spend* (forensics,
        # /budget); the window's occupancy is the last step's prompt, recorded
        # beside it.  Rows written before the reading existed fall back.
        return int(row.context_tokens or row.prompt_tokens or 0)

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
                context_tokens=int(usage.get("context_tokens", 0) or 0),
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
        # Persist the task when it holds items *or* carries orchestration
        # state.  A bare goal is already recoverable from the first user
        # message so writing it would be noise -- but an armed task (or a
        # goal-phase claim) is the boot scan's whole input (D-4 found this:
        # a goal without items is normal, and ``find_armed_session`` reads
        # these raw keys), not noise.  A token account (G1) joins the same
        # list: it is a ledger of spend, and the live run proved the stop
        # path would otherwise drop it with the rest of the block.
        if self.task is not None and (
            self.task.items or self.task.armed or self.task.goal_phase
            or self.task.tokens_used
        ):
            data["task"] = self.task.to_dict()
        # Always carry the key so a later version or human /plan off can express
        # "explicitly cleared" as distinct from a legacy file with no field at
        # all (``from_dict`` only backfills the latter).
        data["approvedPlan"] = self.approved_plan
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
        # Same rule again: sessions that never queued input gain no key.
        if self.inbox:
            data["inbox"] = [item.to_dict() for item in self.inbox]
        # Same rule again.  Without this the boundary survives only in memory:
        # reloading a compacted session brought back the usage rows the
        # compaction had already invalidated, and the meter read them again.
        if self.last_compacted_turn:
            data["lastCompactedTurn"] = int(self.last_compacted_turn)
        # I2: same rule — a session that never compacted writes none of these,
        # so every file written before I2 keeps reading byte-identically.
        if self.compaction_checkpoint:
            data["compactionCheckpoint"] = self.compaction_checkpoint
            data["compactionConsumed"] = int(self.compaction_consumed)
            data["compactionAnchor"] = self.compaction_anchor
        # R20: same rule again -- a session that never read a file writes no key,
        # so every file written before this layer existed reads the same.
        if self.effects.reads:
            data["readLedger"] = [mark.to_dict() for mark in self.read_ledger]
            if any(mark.call_id for mark in self.effects.reads):
                data["readCalls"] = [mark.to_dict() for mark in self.effects.reads]
        if self.effects.files:
            data["fileReceipts"] = [
                {
                    "path": receipt.path,
                    "version": receipt.version,
                    "tool": receipt.tool,
                    "call_id": receipt.call_id,
                    "seq": receipt.seq,
                    "kind": receipt.kind,
                    "confidence": receipt.confidence,
                }
                for receipt in self.effects.files
            ]
        if self.effects.sequence:
            data["effectSeq"] = int(self.effects.sequence)
        if self.completion_override_approved:
            data["completionOverrideApproved"] = True
        data["effectsVersion"] = int(self.effects_version)
        if self.seen_paths:
            data["seenPaths"] = list(self.seen_paths)
        # Deferred tool schemas: only a session that loaded some writes the key.
        if self.loaded_tools:
            data["loadedTools"] = list(self.loaded_tools)
        # P1: the three recovery facts, each only when set, same rule again.
        if self.open_turn is not None:
            data["openTurn"] = int(self.open_turn)
        if self.undo_state is not None:
            data["undoState"] = dict(self.undo_state)
        # Same rule again: a session that never hit a ladder gains no key.
        if self.intervention_state:
            data["interventionState"] = dict(self.intervention_state)
        # M1b T1: the agent is the one stored fact about the posture, and it
        # is written only when it is not the default — so a file written
        # before this layer existed reads the same.
        if self.agent and self.agent != "build":
            data["agent"] = self.agent
        # M1b T1b: the switch history, only when there is one.  A row that is
        # not a mapping is dropped rather than written: the runtime keeps
        # this list, but nothing should be able to write a shape the reader
        # cannot read back.
        history = [dict(row) for row in self.agent_history if isinstance(row, Mapping)]
        if history:
            data["agentHistory"] = history
        # Same rule again (18 §10.1): a writable session gains no key.
        # P1a: plan is read-only by definition, so a session that holds the
        # mismatched pair still lands on disk as the invariant (the read-side
        # normalization is a heal, not the only guard).
        if self.readonly or is_plan_agent(self.agent):
            data["readonly"] = True
        # M1a: same rule again -- a session with no rules of its own gains no
        # key, so every file written before this layer existed reads the same.
        if self.permissions:
            data["permissions"] = [dict(row) for row in self.permissions]
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
        raw_inbox = data.get("inbox")
        inbox_rows = raw_inbox if isinstance(raw_inbox, list) else []
        inbox = [
            item
            for item in (InboxItem.from_dict(row) for row in inbox_rows)
            if item is not None
        ]
        raw_open_turn = data.get("openTurn")
        raw_undo = data.get("undoState")
        raw_intervention = data.get("interventionState")
        raw_permissions = data.get("permissions")
        permission_rows = raw_permissions if isinstance(raw_permissions, list) else []
        permissions = [dict(row) for row in permission_rows if isinstance(row, Mapping)]
        # R20: the read ledger.  A row that is not a mapping, or that is missing
        # any of the three fields, is dropped rather than written back -- the
        # runtime keeps the list, but nothing should be able to write a shape
        # the reader cannot read back (same rule as ``agentHistory``).
        raw_calls = data.get("readCalls")
        raw_ledger = raw_calls if isinstance(raw_calls, list) else data.get("readLedger")
        ledger_rows = raw_ledger if isinstance(raw_ledger, list) else []
        read_marks = tuple(
            mark
            for row in ledger_rows
            if isinstance(row, Mapping)
            and (mark := _read_mark_from_dict(row)) is not None
        )
        raw_receipts = data.get("fileReceipts")
        receipt_rows = raw_receipts if isinstance(raw_receipts, list) else []
        receipts_list: list[FileReceipt] = []
        for row in receipt_rows:
            if not isinstance(row, Mapping) or not row.get("path"):
                continue
            try:
                seq = int(row.get("seq", 0) or 0)
            except (TypeError, ValueError):
                continue
            receipts_list.append(
                FileReceipt(
                    path=str(row["path"]),
                    version=str(row.get("version", "")),
                    tool=str(row.get("tool", "")),
                    call_id=str(row.get("call_id", row.get("callId", "")) or ""),
                    seq=seq,
                    kind=str(row.get("kind", "write") or "write"),
                    confidence=str(row.get("confidence", "exact") or "exact"),
                )
            )
        try:
            effect_seq = int(data.get("effectSeq", 0) or 0)
        except (TypeError, ValueError):
            effect_seq = 0
        max_receipt_seq = max((receipt.seq for receipt in receipts_list), default=0)
        effects = EffectLedger(
            reads=read_marks,
            files=tuple(receipts_list),
            sequence=max(effect_seq, max_receipt_seq),
        )
        raw_effects_version = data.get("effectsVersion")
        try:
            effects_version = int(raw_effects_version or 0)
        except (TypeError, ValueError):
            effects_version = 0
        if raw_effects_version is None and any(
            key in data for key in ("fileReceipts", "effectSeq", "readCalls")
        ):
            # HEAD-era EffectLedger sessions predate effectsVersion; they are
            # not pre-EffectLedger legacy, even though the marker is missing.
            # An *explicit* 0 is a persisted legacy marker and must stay 0.
            effects_version = 2
        raw_seen = data.get("seenPaths")
        seen_rows = raw_seen if isinstance(raw_seen, list) else []
        seen_paths = tuple(
            str(row) for row in seen_rows if isinstance(row, str) and row
        )
        # M1b T1: the agent is the authority.  A file written before the
        # agent table existed carries the old ``mode`` key instead — that is
        # the one migration, and it is on the reading side only.
        raw_agent = data.get("agent")
        if not isinstance(raw_agent, str) or not raw_agent:
            raw_agent = "plan" if str(data.get("mode") or "") == "plan" else "build"
        # P1a: plan ⇒ readonly is an invariant, not a pair of facts a file may
        # disagree about.  A file written before it can hold ``agent="plan"``
        # with a writable overlay; reading it heals the pair, and ``to_dict``
        # then carries only True.  Truthy garbage still coerces to the
        # *restrictive* value: fail-closed.
        raw_readonly = bool(data.get("readonly")) or is_plan_agent(raw_agent)
        raw_history = data.get("agentHistory")
        history_rows = raw_history if isinstance(raw_history, list) else []
        agent_history = [
            dict(row)
            for row in history_rows
            if isinstance(row, Mapping) and row.get("from") and row.get("to")
        ]
        session = cls(
            session_id=data.get("sessionId", ""),
            title=data.get("title", ""),
            created_time=data.get("createdTime", ""),
            last_access_time=data.get("lastAccessTime", ""),
            messages=messages,
            task=TaskState.from_dict(raw_task) if isinstance(raw_task, dict) else None,
            approved_plan=_approved_plan_from_history(messages)
            if data.get("approvedPlan") is None
            else str(data.get("approvedPlan") or ""),
            harvested_messages=int(data.get("harvestedMessages", 0) or 0),
            turn_count=int(data.get("turnCount", 0) or 0),
            turn_digests=digests,
            turn_usages=usages,
            inbox=inbox,
            last_compacted_turn=int(data.get("lastCompactedTurn", 0) or 0),
            compaction_checkpoint=str(data.get("compactionCheckpoint", "") or ""),
            compaction_consumed=int(data.get("compactionConsumed", 0) or 0),
            compaction_anchor=str(data.get("compactionAnchor", "") or ""),
            effects=effects,
            effects_version=effects_version,
            completion_override_approved=(
                data.get("completionOverrideApproved") is True
            ),
            seen_paths=seen_paths,
            loaded_tools=tuple(
                str(name) for name in (data.get("loadedTools") or ()) if name
            ),
            open_turn=int(raw_open_turn)
            if isinstance(raw_open_turn, int) and not isinstance(raw_open_turn, bool)
            else None,
            undo_state=dict(raw_undo) if isinstance(raw_undo, dict) else None,
            intervention_state=(
                dict(raw_intervention) if isinstance(raw_intervention, dict) else None
            ),
            agent=raw_agent,
            agent_history=agent_history,
            # Truthy garbage coerces to the *restrictive* value: fail-closed
            # (and plan is read-only by definition -- see ``raw_readonly``).
            readonly=raw_readonly,
            permissions=permissions,
        )
        # Pre-EffectLedger sessions have no started_seq.  Give every open item
        # a boundary after the loaded sequence so a write performed after this
        # resume can prove it; completed items keep 0 and are not re-gated.
        if session.task is not None:
            boundary = effects.sequence + 1
            for item in session.task.items:
                if not item.is_terminal() and item.started_seq == 0:
                    item.started_seq = boundary
        return session


# Re-export helper for callers that need to resolve references.
__all__ = ["SessionData", "StagedUndo", "TurnUsage", "tool_call_from_ref"]
