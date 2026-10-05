"""Scrolling conversation transcript: message blocks + streaming markdown."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from queue import Empty, SimpleQueue

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.containers import VerticalGroup, VerticalScroll
from textual.widget import AwaitMount, Widget
from textual.widgets import Collapsible, Static

from ai_coding.core.plan_runner import STEP_HEAD
from ai_coding.domain.message import ChatMessage, tool_call_from_ref
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.ui.blocks import (
    Block,
    full_call_view,
    params_preview,
    tool_card_identity,
    tool_card_meta,
    tool_outcome_from_message,
    tool_title,
)
from ai_coding.ui.plain import Plain

_FLUSH_CHARS = 4096


class Prose(Static):
    """Assistant prose: ``rich.markdown`` hosted in a plain ``Static`` (WP4b).

    Textual's ``Markdown`` widget grows asynchronously after mount and the
    outer ``VerticalScroll`` never counts that growth — no scrollbar, dead
    wheel, paint holes, earlier turns unreachable.  A ``Static`` re-renders
    synchronously with a known height, which the scroll accounting handles
    (verified: E1/E3 probes).  ``source`` keeps the markdown text so tests
    and tooling can read back exactly what was streamed.

    Markup is off here too: the prose is handed to ``rich.markdown`` (which
    parses its own syntax), so Textual's parser would only mangle assistant
    text such as ``[strategy-gate]`` or ``call[0]``.
    """

    def __init__(self) -> None:
        super().__init__("", markup=False)
        self.source = ""

    def set_source(self, text: str) -> None:
        self.source = text
        self.update(RichMarkdown(text) if text else "")


@dataclass(frozen=True)
class _ToolFold:
    """One tool call's fold: title parts, live text, finish state.

    ``chunks`` is the thread-safe inbox for streamed output: tool chunks
    arrive on subprocess reader threads with no app context (``NoActiveApp``
    crash), so ``tool_chunk`` only enqueues here.  Rendering (drain + swap)
    happens exclusively on the flush timer / ``end_tool`` / ``backfill``,
    which own a proper UI context.  The queue object is never replaced, so an
    append racing a drain cannot lose an item.
    """

    collapsible: Collapsible
    verb: str
    target: str
    kind: str
    live: str = ""
    chunks: SimpleQueue[str] = field(default_factory=SimpleQueue)
    mark: str | None = None
    status: str | None = None


# Live text cap: the streaming body is UI-only preview (chunks are throttled
# previews by design, R8c); the session file stays the truth.  Same value as
# the backfill cap, but the note must NOT point at the session file — mid-turn
# it does not hold this output yet.
_LIVE_OUTPUT_LIMIT = 20000


def _cap_live(text: str) -> str:
    if len(text) <= _LIVE_OUTPUT_LIMIT:
        return text
    return text[:_LIVE_OUTPUT_LIMIT] + f"\n…（输出过长，仅显示前 {_LIVE_OUTPUT_LIMIT} 字符）"


class Turn(VerticalGroup):
    """One assistant turn: a header, streaming prose, and collapsible tool blocks.

    Content-sized (``VerticalGroup``, NOT ``Vertical``): a ``1fr`` Turn splits
    the scroll track with its siblings, so mounting turn N+1 shrinks turn N's
    box and clips it ("上一轮消失").  With ``height: auto`` each turn owns
    exactly its content rows and the outer scroll accounts all of them.

    Text and tools interleave: each ``start_tool`` finalizes the current prose
    block so subsequent text starts a fresh one.  A tool is a collapsed
    ``Collapsible`` whose body is an exact-height ``Static`` (live preview text
    while running, settled card after backfill); ``end_tool`` swaps the leading
    mark for ✓/✗ and rebuilds the fold.

    The title parts are kept in ``_folds`` because ``ToolFinished`` carries no
    params: the verb/target cannot be re-derived at finish time, so the title
    is rebuilt from what ``start_tool`` was given.

    Precondition: the Turn must already be attached.  ``mount()`` raises
    ``MountError`` on an unattached widget, so every method here that touches
    ``self.mount`` is only safe once ``Transcript`` has mounted the Turn — which
    it does synchronously via ``_register_child``, so a caller never has to
    await anything first.  ``ensure_header`` being called from
    ``Transcript.begin_assistant`` relies on exactly that.
    """

    def __init__(self, timestamp: str) -> None:
        super().__init__()
        self._timestamp = timestamp
        self._pending = ""
        self._prose: Prose | None = None
        self._mount: AwaitMount | None = None
        self._stale: list[tuple[Prose, AwaitMount | None, str]] = []
        self._folds: dict[str, _ToolFold] = {}
        self._header_mounted = False

    def ensure_header(self) -> None:
        # Mounted eagerly rather than in compose(): compose runs one message-loop
        # tick after attach, so a stream() in the same tick would put its
        # Prose above the header.
        if not self._header_mounted:
            self.mount(Plain(_header("✦", "Nexus", self._timestamp), classes="msg-header"))
            self._header_mounted = True

    def append_reasoning(self, text: str) -> None:
        """Mount one collapsed thinking fold (R1-4).

        One fold per call (one per model step): reasoning is completed text,
        never updated in place, so there is nothing to swap — and a static
        mount keeps the scroll accounting exact (same WP4b structural rule as
        prose replacement).  Collapsed because qwen-class thinking runs long;
        the fold, not the wall, is the timeline's presence marker.
        """
        self.ensure_header()
        self.mount(Collapsible(
            Plain(text, classes="reasoning-body"),
            title="思考过程",
            collapsed=True,
            classes="reasoning-fold",
        ))

    def stream(self, text: str) -> None:
        self.ensure_header()
        if self._prose is None:
            self._prose = Prose()
            self._mount = self.mount(self._prose)
        self._pending += text
        if len(self._pending) >= _FLUSH_CHARS:
            self.flush()

    def flush(self) -> bool:
        """Render whatever is buffered; report whether anything was written.

        The report matters: the app drives this from a 50ms timer, and a
        transcript that re-arms its follow on every idle tick will drag a user
        who scrolled up back to the bottom (pinned by
        ``tests/test_transcript_streaming.py::test_overflow_turns_keep_a_working_scroll``).
        """
        wrote = False
        if self._pending and self._prose is not None and self._prose.is_mounted:
            self._replace_prose(self._prose.source + self._pending)
            self._pending = ""
            wrote = True
        for call_id in list(self._folds):
            wrote = self._drain_chunks(call_id) or wrote
        return wrote

    @staticmethod
    def _take_all(queue: SimpleQueue[str]) -> list[str]:
        items: list[str] = []
        while True:
            try:
                items.append(queue.get_nowait())
            except Empty:
                return items

    def _drain_chunks(self, call_id: str) -> bool:
        """Move buffered chunks into the live text and rebuild the fold.

        Runs on the flush timer / ``end_tool`` only — never on the chunk path
        itself, which may execute on a reader thread without an app context.
        Returns whether anything was written (see ``flush``).
        """
        fold = self._folds.get(call_id)
        if fold is None:
            return False
        items = self._take_all(fold.chunks)
        if not items:
            return False
        live = _cap_live(fold.live + "".join(items))
        self._folds[call_id] = replace(fold, live=live)
        self._swap_fold(call_id, Plain(live, classes="tool-live"))
        return True

    def _replace_prose(self, full_text: str) -> AwaitMount | None:
        """Swap the mounted prose for a fresh widget holding ``full_text``.

        WP4b: the scroll container's arrangement cache keys on DOM structure,
        so post-mount growth (``update``/``append``) never reaches its virtual
        size — no scrollbar, dead wheel, paint holes.  A remove+mount pair is
        structural and recomputes.  ``before`` keeps the segment's position
        among tool folds; both operations are scheduled, pump order applies.
        """
        old = self._prose
        assert old is not None
        new = Prose()
        new.set_source(full_text)
        self._prose = new
        mount = self.mount(new, before=old)
        old.remove()
        self._mount = mount
        return mount

    def start_tool(self, call_id: str, verb: str, target: str, kind: str, body: str) -> None:
        self.flush()
        if self._pending:
            # The prose is not mounted yet, so its text could not be flushed;
            # carry it so ``end`` still renders it, before this tool.
            assert self._prose is not None  # pending implies a prose exists
            self._stale.append((self._prose, self._mount, self._pending))
            self._pending = ""
        self._prose = None
        self._mount = None
        collapsible = Collapsible(
            Plain(body, classes="tool-live"),
            title=tool_title(mark="▸", verb=verb, target=target, running=True),
            collapsed=True,
            classes="tool-running",
        )
        self._folds[call_id] = _ToolFold(collapsible, verb, target, kind, live=body)
        self.mount(collapsible)

    def _swap_fold(self, call_id: str, body: Widget) -> None:
        """Rebuild a fold around a new body, preserving title/state/collapse.

        Same structural pattern as ``_replace_prose``: post-mount growth never
        reaches the scroll container's virtual size, so every content change
        is a remove+mount pair on the attached Turn.
        """
        fold = self._folds.get(call_id)
        if fold is None:
            return
        if fold.mark is None:
            title = tool_title(mark="▸", verb=fold.verb, target=fold.target, running=True)
            classes = "tool-running"
        else:
            title = tool_title(mark=fold.mark, verb=fold.verb, target=fold.target)
            classes = f"tool-{fold.status}"
        new = Collapsible(
            body,
            title=title,
            collapsed=fold.collapsible.collapsed,
            classes=classes,
        )
        self._folds[call_id] = replace(fold, collapsible=new)
        self.mount(new, before=fold.collapsible)
        fold.collapsible.remove()

    def tool_chunk(self, call_id: str, text: str) -> None:
        # Buffer only — no widget work.  Chunks arrive on subprocess reader
        # threads whose context has no running loop / active app; constructing
        # or mounting widgets here crashes the app (NoActiveAppError in
        # Collapsible.compose).  Rendering happens in _drain_chunks.
        fold = self._folds.get(call_id)
        if fold is None:
            return
        fold.chunks.put(text)

    def end_tool(self, call_id: str, mark: str, status: str, outcome: str) -> None:
        """Mark the call done, keep its target, and record the result.

        ``outcome`` is the tool's output preview or failure text.  Dropping it
        left a failed call reading as ``✗ read`` with no reason anywhere, so it
        goes into the same log the chunks already went to.  ``status`` becomes
        a class so CSS can tell a ✓ from a ✗ without parsing the glyph.
        """
        tool = self._folds.get(call_id)
        if tool is None:
            return
        self._drain_chunks(call_id)
        tool = self._folds[call_id]  # drain never removes folds
        live = _cap_live(tool.live + outcome) if outcome else tool.live
        self._folds[call_id] = replace(tool, mark=mark, status=status, live=live)
        self._swap_fold(call_id, Plain(live, classes="tool-live"))

    def backfill(self, messages: Sequence[ChatMessage]) -> None:
        """Swap each finished fold for a settled fold with its full session text.

        WP3: events carry only previews by design (R1a-3), so at turn end each
        fold is resolved back against the session messages — the one place the
        untruncated output lives.  The settled fold hosts a ``VerticalGroup``
        card of plain ``Static`` rows: the live body was already an exact-height
        ``Static`` (see ``_swap_fold``), so settling only swaps preview text
        for session text.  The swap is structural (remove+mount on the attached
        Turn, same pattern as ``_replace_prose``), so the scroll container
        recomputes; the user's collapsed/expanded state carries over.  Calls
        with nothing to upgrade, or still running, keep their preview fold;
        an empty ``messages`` is a no-op for old callers.
        """
        for call_id, fold in self._folds.items():
            if fold.mark is None or fold.status is None:
                continue
            view = full_call_view(messages, call_id)
            if view.params is None and view.output is None:
                continue
            rows: list[Widget] = [Plain(fold.kind, classes="tool-kind")]
            if view.params is not None:
                rows.append(Plain(view.params, classes="tool-cmd"))
            if view.output is not None:
                rows.append(Plain(view.output, classes="tool-full"))
            foot = f"{fold.mark} {'成功' if fold.status == 'ok' else '失败'}"
            foot_class = "tool-foot-ok" if fold.status == "ok" else "tool-foot-bad"
            rows.append(Plain(foot, classes=foot_class))
            # VerticalGroup, not Vertical: a 1fr Vertical inside the auto-height
            # fold resolves to fill-available (same disease Turn had), while an
            # auto group is exactly its rows.
            card = VerticalGroup(*rows, classes="tool-card")
            self._swap_fold(call_id, card)

    async def end(self) -> None:
        for prose, mount, pending in self._stale:
            # Pre-mount content counts like constructor content (structural),
            # so no swap is needed here — only mount before returning.
            prose.set_source(prose.source + pending)
            if mount is not None:
                await mount
        self._stale.clear()
        if self._pending and self._prose is not None:
            full = self._prose.source + self._pending
            if self._prose.is_mounted:
                mount = self._replace_prose(full)
                if mount is not None:
                    await mount
            else:
                self._prose.set_source(full)
                if self._mount is not None:
                    await self._mount
            self._pending = ""
        self._folds.clear()


class StepView:
    """One plan step's live body: buffered prose inside a Collapsible.

    Same growth discipline as a tool fold and for the same reason (WP4b): the
    scroll container's arrangement cache keys on DOM structure, so the body is
    replaced (remove+mount) rather than updated in place, or the view grows
    without the scrollbar knowing.  Buffering follows ``Turn``: deltas arrive
    as tokens and only a flush renders, so a chatty step does not thrash the
    DOM.
    """

    def __init__(self, fold: Collapsible, body: Prose,
                 on_end: Callable[[], None] | None = None) -> None:
        self._fold = fold
        self._body: Prose | None = body
        self._pending = ""
        self._on_end = on_end

    def delta(self, text: str) -> None:
        """Take one streamed token."""
        self._pending += text

    def flush(self) -> bool:
        """Render what is buffered; report whether anything was written."""
        if not self._pending:
            return False
        old = self._body
        if old is not None and old.parent is None:
            # The Collapsible has not composed its body yet (compose runs a
            # message-loop tick after attach, and a fast step can flush before
            # that).  Textual's Collapsible only takes children through its
            # constructor — mounting afterwards would land outside the
            # Contents container — so wait one refresh and keep the text
            # buffered rather than writing it somewhere it will not show.
            # Through the fold: StepView is not a widget, the fold is.
            self._fold.call_after_refresh(self.flush)
            return False
        assert old is not None  # guarded by the parent check above
        text, self._pending = self._pending, ""
        new = Prose()
        new.set_source(old.source + text)
        # remove+mount, not in-place: the scroll container's arrangement keys on
        # DOM structure, so growing a widget in place never reaches its virtual
        # size (WP4b).  `before=old` keeps the body's place among its siblings.
        self._fold.mount(new, before=old)
        old.remove()
        self._body = new
        return True

    def end(self, summary: str | None = None) -> None:
        """Close the step: final flush, and the title becomes the receipt.

        The title is where the outcome lives for the live view, because the
        step's own transcript is gone by design and only the summary is
        persisted — so this is the same sentence /resume will rebuild from the
        receipt.  If the body never got composed (a step that produced no
        deltas at all), the text is dropped rather than written blind: there is
        nothing to show.
        """
        self.flush()
        if summary:
            self._fold.title = f"{self._fold.title} — {summary}"
        if self._on_end is not None:
            self._on_end()


class Transcript(VerticalScroll):
    """Append-only conversation area with a "stay at the bottom" policy.

    Streaming is two-phase because ``on_delta`` is a *sync* callback:
    ``stream`` only buffers, ``flush`` (driven by a 50ms timer in the app)
    renders, and the async ``end_assistant`` awaits the mount before the
    final write (spec §3.3-1).
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._turn: Turn | None = None
        self._last_finished: str | None = None
        self._step_view: StepView | None = None
        # "Is the user still with us?" — asked of Textual's anchor, which keeps
        # an anchored widget at the bottom as content arrives, but the question
        # itself is ours: the anchor cannot be re-armed once the user has left.
        # See watch_scroll_y for why this is decidable at all.
        self._pinned = True

    @property
    def last_finished(self) -> str | None:
        """Call id of the most recently finished tool call (any outcome)."""
        return self._last_finished

    def append_user(self, text: str, timestamp: str) -> None:
        self.mount(Plain(_header("◯", "You", timestamp), classes="msg-header"))
        self.mount(Plain(text, classes="user-bubble"))
        self._follow()

    def begin_assistant(self, timestamp: str) -> None:
        self._turn = Turn(timestamp)
        self.mount(self._turn)
        self._turn.ensure_header()
        self._follow()

    def stream(self, text: str) -> None:
        if self._turn is None:
            self._turn = Turn(_clock())
            self.mount(self._turn)
        self._turn.stream(text)
        self._follow()

    def flush(self) -> None:
        """Called on the app's 50ms timer, so it must be free when idle.

        A running plan step is flushed on the same cadence as assistant prose —
        otherwise the step's words would sit in the buffer until the step ended,
        which is the invisibility M3 exists to remove.
        """
        wrote = False
        if self._turn is not None:
            wrote = self._turn.flush()
        step = self._step_view
        if step is not None:
            wrote = step.flush() or wrote
        if wrote:
            self._follow()

    async def end_assistant(self, messages: Sequence[ChatMessage] = ()) -> None:
        turn = self._turn
        self._turn = None
        if turn is not None:
            turn.backfill(messages)
            await turn.end()
        self._follow()

    def append_block(self, block: Block) -> None:
        turn = self._turn
        if turn is not None and block.kind == "tool":
            if block.tool_status == "running":
                turn.start_tool(
                    block.call_id,
                    *_fold_identity(block),
                    block.text,
                )
            elif block.tool_status in {"ok", "failed"}:
                mark = "✓" if block.tool_status == "ok" else "✗"
                turn.end_tool(block.call_id, mark, block.tool_status, block.text)
                self._last_finished = block.call_id
            else:
                self.mount(Plain(block.render(), classes=f"block-{block.kind}"))
        elif turn is not None and block.kind == "tool-chunk":
            turn.tool_chunk(block.call_id, block.text)
        elif turn is not None and block.kind == "reasoning":
            turn.append_reasoning(block.text)
        else:
            self.mount(Plain(block.render(), classes=f"block-{block.kind}"))
        self._follow()

    def clear(self) -> None:
        for child in list(self.children):
            child.remove()
        self._turn = None
        # Whatever the user was reading is gone, so there is nothing to stay
        # scrolled up to: a rebuilt transcript follows again.
        self._pinned = True
        self.anchor()
        # Whatever the user was reading is gone, so there is nothing to stay
        # scrolled up to: a rebuilt transcript starts following again.
        self.anchor()

    async def replay_history(
        self,
        messages: Sequence[ChatMessage],
        deliveries: Sequence[StepDelivery] = (),
    ) -> None:
        """Rebuild the transcript from a loaded session, for ``/resume``.

        Walks the persisted messages in order — user bubbles, assistant prose,
        settled tool cards — through the same ``Turn`` machinery the live view
        uses, so a reopened card reads exactly like the card that settled at
        the time (same titles, same caps, collapsed).  Nothing is streamed:
        old sessions keep only final ``ToolResult`` values, never the live
        chunks, so replaying a stream would be fiction.  An orphan tool call
        (no matching ``role=tool`` message) is skipped rather than rendered
        half — ``ensure_tool_pairing`` should have removed it, this is the
        belt to its suspenders.

        Plan steps recorded by ``/plan-run`` reappear as headers, matched to
        the step's own user bubble by prompt marker (turn numbers do not
        survive compaction, so numbers are not trusted).  A delivery whose
        bubble was compacted away is skipped, not invented.
        """
        self.clear()
        results: dict[str, ChatMessage] = {}
        for message in messages:
            if message.role == "tool" and message.tool_call_id:
                results[message.tool_call_id] = message  # later wins, as in full_call_view
        by_statement = {entry.statement: entry for entry in deliveries}

        async def close_turn() -> None:
            turn = self._turn
            self._turn = None
            if turn is not None:
                turn.backfill(messages)
                await turn.end()

        for message in messages:
            if message.role == "user":
                if not message.content:
                    continue
                await close_turn()
                header = _step_header(message.content, by_statement)
                if header is not None:
                    self.mount(Plain(header, classes="plan-step"))
                self.append_user(message.content, _hhmm(message.timestamp))
            elif message.role == "assistant":
                if not message.content and not message.tool_calls:
                    continue
                if self._turn is None:
                    self.begin_assistant(_hhmm(message.timestamp))
                assert self._turn is not None
                if message.content:
                    self._turn.stream(message.content)
                    self._turn.flush()
                for ref in message.tool_calls or []:
                    result = results.get(ref.id)
                    if result is None:
                        continue
                    call = tool_call_from_ref(ref)
                    verb, target, kind = tool_card_identity(call.name, call.params)
                    self._turn.start_tool(ref.id, verb, target, kind, params_preview(call.params))
                    success, text = tool_outcome_from_message(result)
                    mark = "✓" if success else "✗"
                    self._turn.end_tool(ref.id, mark, "ok" if success else "failed", text)
                    self._last_finished = ref.id
                # R1-4: thinking rides after the step's prose and folds, live
                # and replayed alike — the event only exists complete at step
                # end, so "thought first" would be fiction either way.
                if message.reasoning:
                    self._turn.append_reasoning(message.reasoning)
        await close_turn()
        self._follow()

    def begin_plan_step(self, title: str) -> StepView:
        """Open a fold for one plan step's live output (M3).

        Collapsed, like a tool fold: a step's prose is bounded but long, and
        the title is the step's presence in the timeline.  Not part of the
        current turn — the planning turn's assistant block stays intact, and
        the step's words never enter the parent's transcript (M1: a step is a
        short conversation, and replay reconstructs the timeline from
        receipts, not from these bodies).
        """
        # The body goes in through the constructor because Textual's Collapsible
        # only adopts children that way (compose_add_child); it is a tick later
        # before that body has a parent, which StepView.flush waits out.  Our own
        # reference is what we swap, not a query — the fold has not composed yet.
        body = Prose()
        fold = Collapsible(body, title=title, collapsed=True,
                           classes="plan-step-fold")
        self.mount(fold)
        self._follow()
        view = StepView(fold, body, self._forget_step_view)
        self._step_view = view
        return view

    def _forget_step_view(self) -> None:
        """The step closed: stop flushing it (the next step opens its own)."""
        self._step_view = None

    def _follow(self) -> None:
        """Stay at the bottom until the user scrolls away.

        This asks Textual for the *anchor* instead of sampling
        ``is_vertical_scroll_end`` and scrolling once.  Sampling after a mount
        cannot deliver the policy: the mounted widget has no height until the
        layout that follows, so the answer describes a view that no longer
        exists.  Measured on the one-shot version — a 60-block burst ended with
        the scroll target at 0 of 36 (it never scrolled at all, because the
        burst had already made the view look scrolled-away), and a 30-turn
        /resume replay ended showing the middle of the conversation.  Both are
        one bug seen from two sides.

        ``anchor()`` is the policy already implemented: the compositor keeps an
        anchored widget at the bottom on every arrange, and releases the anchor
        when the user moves the scroll (re-arming when they come back).  What
        it cannot do is re-arm itself for a user who has left — hence
        ``_pinned``.
        """
        if self._pinned:
            self.anchor()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Landing above the bottom we asked for means the user scrolled away.

        This is decidable because of *how* the three scroll sources differ:
        our own ``anchor()`` always lands on ``max_scroll_y``, and the
        compositor's keep-at-bottom writes go through ``set_reactive``, which
        bypasses watchers — so the only scrolls that arrive here and stop short
        of the bottom are the user's.  That also means the transient "content
        grew under us" case cannot unpin us, which is exactly what made the
        old ``is_vertical_scroll_end`` sample undecidable.
        """
        super().watch_scroll_y(old_value, new_value)
        if new_value < self.max_scroll_y - 1:
            self._pinned = False


def _step_header(
    content: str, by_statement: dict[str, StepDelivery]
) -> Text | None:
    """A plan-step header for a user bubble that starts a step (R1-1).

    Matches on the runner's prompt marker: the bubble text travels inside the
    persisted user message, so this survives everything except compaction
    removing the bubble itself (then there is nothing to head, and nothing
    is invented).  Returns a ``Text`` (never markup-parsed) or ``None``.
    """
    if not content.startswith(STEP_HEAD):
        return None
    statement = content[len(STEP_HEAD):].split("\n")[0]
    entry = by_statement.get(statement)
    if entry is None:
        return None
    mark, word = {
        "completed": ("✓", "完成"),
        "blocked": ("✗", "受阻"),
        "stalled": ("…", "停滞"),
    }.get(entry.status, ("·", entry.status))
    text = Text()
    text.append(f"{mark} 步骤 {entry.step_id}：{entry.statement}（{word}）", style="bold")
    return text


def _fold_identity(block: Block) -> tuple[str, str, str]:
    """(verb, target, kind) for a fold title, with a tool-name fallback.

    ``blocks_from_event`` always fills verb/kind; the fallback only serves
    hand-built blocks (tests), resolved through the same table.
    """
    if block.verb and block.kind_label:
        return block.verb, block.tool_target, block.kind_label
    kind, verb = tool_card_meta(block.tool_name)
    return verb, block.tool_target, kind


def _clock() -> str:
    return datetime.now().strftime("%H:%M")


def _hhmm(timestamp: str) -> str:
    """``HH:MM`` for a persisted message timestamp.

    Session stamps are ISO-8601 (``2026-10-03T08:01:00Z``); anything else —
    the ``ts``/``ts1`` stamps unit tests use — passes through untouched, so a
    replayed header shows when the words were said, not when they reload.
    """
    if isinstance(timestamp, str) and len(timestamp) >= 16 and timestamp[10] == "T":
        return timestamp[11:16]
    return timestamp or ""


def _header(icon: str, name: str, timestamp: str) -> Text:
    text = Text()
    text.append(f"{icon} ", style="bold blue")
    text.append(name, style="bold")
    text.append(f"  {timestamp}", style="dim")
    return text
