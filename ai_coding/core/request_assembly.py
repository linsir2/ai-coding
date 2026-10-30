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
from pathlib import Path
from typing import Any

from ai_coding.ai.llm_helpers import checkpoint_body
from ai_coding.config.models import DEFAULT_CONTEXT_TOKENS
from ai_coding.core.compaction import Compactor
from ai_coding.core.context_compression import (
    chars_for_tokens,
    estimate_tokens,
    working_history_tokens,
)
from ai_coding.core.events import ContextCompacted, LoopEvent, MemoryInjected
from ai_coding.core.file_mentions import resolve_bare_paths, resolve_file_mentions
from ai_coding.core.plan_reminders import reconcile
from ai_coding.core.plan_runner import outside_workspace_paths
from ai_coding.core.request_builder import build_tail, content_of, volatile_block
from ai_coding.core.sop_router import SOPRouter
from ai_coding.core.turn_books import TurnBooks
from ai_coding.domain.message import CHECKPOINT_MARK, IMAGE_MIME_BY_SUFFIX, ChatMessage
from ai_coding.domain.read_ledger import render as render_read_ledger
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import render_working_memory
from ai_coding.infra.deadline import Deadline
from ai_coding.infra.time_utils import now_local
from ai_coding.skills.policy import denied_skill_ids
from ai_coding.skills.registry import SkillRegistry, iter_mentions, resolve_mentions
from ai_coding.skills.render import render_skill_content

__all__ = ["Assembled", "RequestAssembler", "human_utterance"]

#: r17/T4 caps, now expressed in the unit the window is measured in (R22/H10).
#:
#: These used to be character constants (20,000 per file, 40,000 per block)
#: whatever ``max_context_tokens`` was.  A 6k window therefore received 40,138
#: characters on every iteration -- 96% of the request -- and because the block
#: is appended *after* compression, the pipeline could never shrink it: S3
#: compacted three times in one turn and still ran at 2.7x the window.
#:
#: The block now gets 15% of the window in tokens.  ``10,000`` is the ceiling the
#: old 40,000 **ASCII** characters stood for (a Chinese-heavy file reached ~20k
#: tokens at the same character count, which is exactly the over-admission this
#: change removes), so a large window cannot grow the block without bound.
#: ``estimate_tokens`` turns a token budget into a character cut, and the min is
#: a per-file floor so one big paste cannot reduce the next file to a pointer.
_FILES_BLOCK_RATIO = 0.15
_FILES_BLOCK_TOKENS_CAP = 10_000
_FILES_BLOCK_MIN_TOKENS = 150
_FILE_TOKENS_CAP = 5_000
_DIR_ENTRIES = 200


def human_utterance(messages: Sequence[ChatMessage]) -> str:
    """The last thing a *human* wrote -- one criterion for every reader.

    One kind of line in the transcript is not the human's: a **synthetic**
    message (r18/F3) -- a nudge, a steering line, a task state block, the
    plan reminder (M2 T3), the planning instruction (M2 T3, Q4) -- the
    harness talking in the user's shape.

    The planning instruction used to need its own exclusion, because it was
    written in the user's voice and told apart by its ``[plan] `` prefix.
    It is synthetic now, so the one rule covers it -- and a reader no longer
    has to know what a prefix means.

    Everything else is the human's, and it is the input the mention blocks
    are rebuilt from, the input the outside-workspace warning looks at, and
    the request the research exit judges.

    R22/A-15: a **checkpoint** is stored as a user-role message too (it has to
    be -- the model reads it as the conversation so far), and it is *model*
    text.  Before this, `/compact` made the summary "the last thing a human
    wrote", so the file-reference block, the skill mentions and the
    outside-workspace check were all rebuilt from a summary.  The test here is
    the one `split_checkpoint` already uses -- the marker plus
    `checkpoint_body`'s shape -- not a second prefix rule.
    """
    for message in reversed(messages):
        if message.role != "user" or message.synthetic:
            continue
        content = (message.content or "").strip()
        if _is_checkpoint_text(content):
            continue
        return content
    return ""


def _is_checkpoint_text(content: str) -> bool:
    """True when this user-role line is a compaction checkpoint, not a person."""
    if not content.startswith(CHECKPOINT_MARK):
        return False
    return checkpoint_body(content[len(CHECKPOINT_MARK) :]) is not None


def _carries_checkpoint(messages: Sequence[ChatMessage]) -> bool:
    """True when a summary already stands in for earlier turns of this history.

    R22/H11: ``/compact`` replaces ``session.messages`` itself, so
    ``did_compact`` (a comparison against the session's own list) is False on
    every later turn — the one case where the checkpoint is *persistent* is the
    one case the "the summary may be unreliable" caveat would have missed.
    """
    return any(
        message.role == "user" and _is_checkpoint_text((message.content or "").strip())
        for message in messages
    )


def _file_tag(rel: str) -> str:
    """The opening tag of a file block, for measuring whether one can still fit."""
    return f'<file path="{rel}">\n'


def _file_content(rel: str, target: Path, *, room_tokens: int) -> str:
    """One file's text, wrapped, cut to ``room_tokens``.

    ``room_tokens`` is whichever ceiling is tighter: the per-file cap, or what is
    left of the block budget.  The cut lands on a line boundary and the note
    names the range it holds plus the ``read`` call that continues it -- S3's
    model read a 497-line head of a 6000-line log and concluded the file was
    complete, because the old note only said how many *characters* existed.
    """
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f'<file path="{rel}">[unreadable: {exc}]</file>'
    limit = chars_for_tokens(text, room_tokens)
    if limit >= len(text):
        return f'<file path="{rel}">\n{text}\n</file>'
    cut = text[:limit]
    # Land on a line boundary so ``offset`` below names a whole line (a leading
    # blank line is boundary 0 and must be kept, not mistaken for "no newline").
    boundary = cut.rfind("\n")
    if boundary >= 0:
        cut = cut[: boundary + 1]
    # Split the same way ``read`` does (splitlines counts \x0c and \u2028 too),
    # otherwise the note's line numbers drift from the tool's offsets.
    total = len(text.splitlines())
    if cut.count("\n") == 0:
        shown = 0
    else:
        shown = len(cut.splitlines())
    if shown == 0:
        # A file with no line break in the shown part cannot be paged by
        # ``read``'s line offset; saying "lines 1-0, offset=0" would send the
        # model back to the beginning.
        note = (
            f"\n[truncated: {len(text)} chars, {total} line{'s' if total != 1 else ''} total; "
            f"this text is the first {len(cut)} chars of line 1]"
        )
    else:
        # ``read``'s offset is 0-based, so "already shown lines 1-N" continues at
        # offset=N -- exactly the number in this note.
        note = (
            f"\n[truncated: {len(text)} chars, {total} lines total; this text is lines 1-{shown}; "
            f"read offset={shown} limit=500 for the rest]"
        )
    return f'<file path="{rel}">\n{cut}{note}\n</file>'


def _bare_pointer(rel: str, target: Path) -> str:
    """One line for a path the prompt merely names: name the file, not its bytes.

    ``resolve_bare_paths`` cannot tell "look at this file" from "this file exists
    and is part of the sentence", so inlining the body of every name turned a
    wave seed into 22-28k characters of ``<referenced-files>`` per request --
    paid on every iteration, unreachable by compaction (it is a volatile tail
    block), and larger than the history the pipeline was busy shrinking.

    The read tool is one call away and asks for approval on its own; a pointer is
    enough.  An explicit ``@file`` keeps the inline: that is a request to paste.
    """
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"- {rel}: [unreadable: {exc}]"
    lines = len(text.splitlines())
    return (
        f"- {rel} ({lines} line{'s' if lines != 1 else ''}): "
        "[not inlined this request; read it with the read tool]"
    )


def _observed_prompt_tokens(session: SessionData) -> int:
    """The best measurement of the request that is about to be sent.

    The compaction gate used to read only ``estimate_tokens``, which counts
    characters and underestimated a tool-output-heavy history by ~4x on a real
    session (60k estimated against 237,157 billed). The provider's number is the
    ground truth, so it is what the gate gets.

    Two sources, in order:

    * the running turn's own step reading (``live_prompt_tokens``) — the
      provider measured the request this turn actually sent;
    * ``working_history_tokens`` — what the *compacted* history would weigh,
      used before the first step has reported.  Measuring the raw
      ``session.messages`` instead made the gate open on every iteration once
      the append-only history crossed the ceiling (2026-10-30 live run).

    The boundary question is the session's to answer, not this module's: a row at
    or before the last compaction measured a context that no longer exists, and
    reading it would make the gate compact again on the very next request.
    """
    live = session.live_prompt_tokens()
    if live > 0:
        return live
    return working_history_tokens(session)


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
        # R01 (F-51): the loop replaces ``policy`` for a planning round or a
        # sub-agent (``continuations_disabled``) or a plan step (a scoped
        # policy of its own).  A provider makes every iteration read the policy
        # actually in force instead of the object this assembler happened to be
        # built with; absent, the passed-in policy is the answer (bare
        # constructors, tests).
        policy_provider: Callable[[], Any] | None = None,
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
        workspace: str = "",
        extra_workspace_roots: Sequence[str] = (),
    ) -> None:
        self._compactor = compactor
        self._policy = policy
        self._policy_provider = policy_provider
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
        # r17/T4: where @path references resolve.  Empty means the feature is
        # off for this assembler (old constructors, tests) -- never an error.
        self._workspace = workspace
        # B/D5-c: the roots the operator declared (``tools.extra_workspace_roots``),
        # already expanded by the composition root.  Passed in rather than read
        # off ``settings`` because ``AIConfig`` is not ``ToolsConfig``: reaching
        # across the two would be a lookup by accident.
        self._extra_roots = tuple(extra_workspace_roots)

    def _active_policy(self) -> Any:
        """The policy in force for this request (R01): the provider's answer
        when one was handed in, the construction-time object otherwise."""
        if self._policy_provider is not None:
            return self._policy_provider()
        return self._policy

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
        if getattr(self._settings, "auto_compact", True):
            history = await self._compactor.compress(
                session.messages,
                deadline=deadline,
                on_summary=books.summaries.append,
                pinned=pinned,
                **self._compactor_extras(session),
            )
        else:
            # settings.auto off (OpenCode parity): the budget-driven path is
            # silenced here, the single proactive entry point.  /compact and
            # the overflow rescue call the compactor directly and still work.
            history = session.messages
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
        # M2 T3: reconcile the plan reminder with the posture, every request.
        # A compaction may have eaten it (put it back) or the session may
        # have left plan mode without the withdrawal surviving (say so).
        # The reminder joins the request's view of the history -- it is
        # synthetic, so the readers that want the human's words skip it.
        reminder = reconcile(history, is_plan=str(session.mode) == "plan")
        if reminder:
            history = [
                *history,
                ChatMessage(
                    role="user",
                    content=reminder,
                    timestamp=now_local(),
                    synthetic=True,
                ),
            ]
        instructions = (
            await self._build_instructions(deadline, session)
            if self._build_instructions is not None
            else None
        )
        injection = self._active_policy().inject_task(task, instructions)
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
        pressure = self._active_policy().budget_pressure(
            iteration=index, tokens_spent=self._reporter.tokens_spent()
        )
        # M2: recommend skills based on current context (after task, before hook).
        recommended_skills_block = ""
        recovery_block = ""
        user_input = self._extract_user_input(session)
        if self._router is not None:
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
            # person rather than a heuristic — and (r16/E-3) it arrives loaded:
            # the full content rides this tail block, so the turn needs no
            # round trip through the tool. The block is volatile: rebuilt from
            # the last user message every iteration, gone with the next one.
            requested = resolve_mentions(user_input, self._skills)
            if requested:
                recommended_skills_block += "\n\n" + self._requested_skills_block(requested)
        # r17/T4: @path references ride their own volatile block -- independent
        # of the router, because a workspace can hold files without skills.
        file_block = self._referenced_files_block(user_input)
        # I3: inject anchor after compaction
        anchor_block = self._format_anchor_block(
            session, compacted or _carries_checkpoint(history)
        )
        # M2: the catalog sits with the other state to read, before the
        # instructions — it is reference data, not an instruction.
        catalog = self._catalog_builder() if self._catalog_builder is not None else ""
        environment_text = self._environment_block(session)
        tail, tail_blocks = build_tail(
            [
                volatile_block("environment", environment_text),
                volatile_block("memory", memory_block),
                # R20/RC-2: the read ledger.  A volatile block, so it is rebuilt
                # from session state every iteration and compaction cannot reach
                # it -- the checkpoint stores conclusions, this stores evidence.
                volatile_block("read_ledger", render_read_ledger(session.read_ledger)),
                volatile_block("mcp_catalog", catalog),
                volatile_block("task", content_of(injection.message)),
                volatile_block("recommended_skills", recommended_skills_block),
                volatile_block("referenced_files", file_block),
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

    def _environment_block(self, session: SessionData) -> str:
        """F (13 §2.4): the hard facts every thought must be grounded in.

        Volatile like the other tail blocks -- rebuilt from the session every
        iteration, never persisted.  The workspace line is always present; the
        branch line is omitted when git has nothing to say; the moment line
        (C-4) carries the local time *with its offset* and needs no cache
        throttling, because the tail sits after the history and never enters
        it -- the cached prefix is unchanged by a clock that ticks.
        """
        lines = [f"工作区: {self._workspace}"]
        branch = getattr(session, "git_branch", "")
        if branch:
            lines.append(f"分支: {branch}")
        lines.append(f"时间: {now_local()}")
        # hotel-fix P0-1 Build补洞：用户首轮提到工作区外绝对路径时警告
        # （只读提示，不拒绝；复用 plan_runner.outside_workspace_paths，
        # 不复制正则；synthetic 消息由 _extract_user_input 跳过）。
        if self._workspace:
            user_input = self._extract_user_input(session)
            if user_input:
                offenders = outside_workspace_paths(
                    [user_input], self._workspace, extra_roots=self._extra_roots
                )
                if offenders:
                    shown = "、".join(offenders[:3])
                    lines.append(
                        f"注意: {shown} 在工作区外，文件工具无法访问；"
                        "请用 -w 切换到目标仓库，或把需求改为工作区内相对路径。"
                    )
        # D-7: one line, and only where there is something to recall.  A session
        # whose history was never rewritten pays nothing for this; a compacted
        # one is told where its exact wording went, every request, in the
        # volatile tail -- never persisted, never in the history.
        if getattr(session, "last_compacted_turn", 0):
            lines.append(
                "历史: 早期对话已被压缩为摘要；需要原文措辞、路径或数字时，"
                "用 history_search 取回。"
            )
        return "<environment>\n" + "\n".join(lines) + "\n</environment>"

    def _referenced_files_block(self, user_input: str) -> str:
        """The user's file references: ``@path`` tokens, and paths they pasted.

        Volatile, like the skills mention: rebuilt from the last user message
        every iteration, gone with the next message.  A token a skill already
        claimed is left to the skill path (instructions win over a file that
        shares a name).

        Both resolvers return workspace-relative paths, so the rest of this
        method stays one code path for both -- one budget, one truncation rule,
        one directory listing.  Two blocks would duplicate all three and let
        them drift, which is why a pasted path shares this method with ``@``
        instead of getting its own.
        """
        if not self._workspace:
            return ""
        claimed: set[str] = set()
        if self._skills is not None:
            claimed = {
                token
                for token in iter_mentions(user_input)
                if self._skills.resolve(token) is not None
            }
        explicit = list(
            resolve_file_mentions(user_input, self._workspace, exclude=claimed)
        )
        # A bare path is a name, not a paste request (see ``_bare_pointer``).
        # An ``@file`` that is also named bare stays explicit.
        bare = [
            path
            for path in resolve_bare_paths(user_input, self._workspace, exclude=claimed)
            if path not in set(explicit)
        ]
        paths = list(dict.fromkeys([*explicit, *bare]))
        explicit_paths = set(explicit)
        if not paths:
            return ""
        root = Path(self._workspace).expanduser().resolve()
        lines = [
            "<referenced-files>",
            "用户引用了以下文件/目录（@ 提及或直接粘贴路径），内容如下：",
        ]
        used = 0
        budget = self._files_block_tokens()
        # Every named file keeps a floor of `_FILES_BLOCK_MIN_TOKENS` while
        # others are still to come, so one large paste cannot turn the 21-byte
        # file after it into a bare pointer (measured: it did, before this).
        pending = sum(
            1
            for rel in paths
            if rel in explicit_paths
            and Path(rel).suffix.lower() not in IMAGE_MIME_BY_SUFFIX
            and not (root / rel).is_dir()
        )
        for rel in paths:
            # r17/T5: an image mention rides as a message part (the loop reads
            # its bytes); injecting it as text would be noise at best.
            if Path(rel).suffix.lower() in IMAGE_MIME_BY_SUFFIX:
                continue
            target = root / rel
            if target.is_dir():
                block = self._directory_listing(rel, target)
            elif rel not in explicit_paths:
                # Named, not attached: a pointer plus the read tool's name.
                block = _bare_pointer(rel, target)
            else:
                pending -= 1
                room = min(_FILE_TOKENS_CAP, budget - used - pending * _FILES_BLOCK_MIN_TOKENS)
                if room <= 0:
                    # D5: a file the user named is asked for, so the budget
                    # decides how much of it the model reads -- never whether it
                    # reads it.  Out of room here now means "a pointer with the
                    # read call", not "a fragment of three hundred characters".
                    lines.append(
                        f"- {rel}: [no room left in this request's file budget; "
                        "read it with the read tool instead]"
                    )
                    continue
                block = _file_content(rel, target, room_tokens=room)
            used += estimate_tokens(block)
            lines.append(block)
        lines.append("</referenced-files>")
        return "\n".join(lines)

    def _files_block_tokens(self) -> int:
        """This block's budget, in tokens, from the window the pipeline uses.

        Read from the compactor rather than from ``settings``: the window is what
        the pipeline compacts against (``/model`` can repoint it), and
        ``AIConfig`` on its own has no ``ai``/``models`` section for
        ``resolve_context_window`` to read -- calling that here silently returned
        the 48k default and would have left a 6k session unfixed.
        """
        window = int(getattr(self._compactor, "context_window", None) or DEFAULT_CONTEXT_TOKENS)
        scaled = int(window * _FILES_BLOCK_RATIO)
        return max(_FILES_BLOCK_MIN_TOKENS, min(_FILES_BLOCK_TOKENS_CAP, scaled))

    @staticmethod
    def _directory_listing(rel: str, target: Path) -> str:
        try:
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        except OSError as exc:
            return f'<file path="{rel}" type="dir">[unreadable: {exc}]</file>'
        shown = entries[:_DIR_ENTRIES]
        note = f"\n[+{len(entries) - _DIR_ENTRIES} more]" if len(entries) > _DIR_ENTRIES else ""
        return f'<file path="{rel}" type="dir">\n' + "\n".join(shown) + note + "\n</file>"

    def _requested_skills_block(self, requested: Sequence[str]) -> str:
        """The named skills' full content, the same shape a read returns.

        A broken skill says so instead of injecting a half-parsed body — the
        tool would have returned the same verdict, one call later. A denied
        one (E-5) is withheld: the operator's boundary beats the user's
        naming, exactly as it beats the model's own call.
        """
        denied = denied_skill_ids(
            self._skills, getattr(self._settings, "skill_permissions", None)
        )
        lines = [
            "<requested-skills>",
            "用户显式指定了以下 skill，完整内容已加载，无需再调用 skill 工具读取：",
        ]
        for name in requested:
            if name in denied:
                lines.append(f"- {name}: 已被配置禁止（skill_permissions: deny），不要读取。")
                continue
            skill = self._skills.resolve(name) if self._skills is not None else None
            if skill is None:  # unreachable: resolve_mentions yields resolved names
                lines.append(f"- {name}: 无法加载。")
                continue
            if not skill.is_healthy:
                lines.append(f"- {name}: 无法加载（{skill.error}）。")
                continue
            lines.append(render_skill_content(skill))
        lines.append("</requested-skills>")
        return "\n".join(lines)

    @staticmethod
    def _extract_user_input(session: SessionData) -> str:
        """The last message a *human* wrote, which is what the mention blocks are
        rebuilt from.

        The criterion lives in :func:`human_utterance` -- one answer for every
        reader (the mention blocks, the outside-workspace warning, and the
        research exit's own gate).
        """
        return human_utterance(session.messages)

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
        """Format pre-compaction anchor as tail block.

        R22/H11: the caveat rides this block rather than a new one, because this
        is the only block that exists *only* after a compaction -- the moment the
        model starts trusting a summary it cannot recompute.  S3 answered "8
        lines (truth 162)" from a checkpoint that claimed all 12 chunks were read;
        the summariser had only ever seen 1,000-character head/tail slices of each
        tool result, so its count was never verifiable.  Keyed on ``compacted``
        alone: an anchor with no task goal (no plan/goal run) still needs it.
        """
        anchor = getattr(session, "pre_compaction_anchor", "")
        if not compacted:
            return ""
        lines = ["<task-anchor>"]
        if anchor:
            lines.append(f"压缩前的任务锚点：{anchor}")
        lines.append(
            "摘要可能不完整：若结论依赖计数、聚合或逐项枚举，请用一条命令重新核算"
            "（如 grep -c / wc -l），不要只依据摘要。"
        )
        if anchor:
            lines.append("如果当前工作偏离此目标，请回到原始任务。")
        lines.append("</task-anchor>")
        return "\n".join(lines)

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
