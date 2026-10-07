I have everything I need. Here is the analysis.

---

# ORCHESTRATION LAYER ANALYSIS — `ai_coding/`

All paths are absolute from `/home/linsir365/projects/ai-coding/`.

---

## 1. ENTRY POINTS

### 1.1 The single programmatic entry: `AgentLoop.process_input`

`ai_coding/core/agent_loop.py:253-259`:

```python
    async def process_input(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
        observer: LoopObserver | None = None,
    ) -> str:
        """Run one user turn to completion and return the final assistant text."""
```

### 1.2 All callers of `process_input` (complete list)

| Caller | Location | Trigger |
|---|---|---|
| TUI `run_turn` worker | `ai_coding/ui/textual_app.py:351-353` | plain text, Enter |
| REPL dispatcher | `ai_coding/ui/repl.py:79` | plain text on terminal |
| REPL `/review` | `ai_coding/ui/repl.py:179-184` | slash command `review` |
| Plan authoring turn | `ai_coding/core/plan_runner.py:101-102` | `/plan` |
| Plan step turn | `ai_coding/core/plan_runner.py:484-487` | one bounded turn per plan step |
| Plan repair turn | `ai_coding/core/plan_runner.py:496-499` | one repair per failing step |
| Sub-agent child turn | `ai_coding/core/subagent.py:473-477` | `subAgent` tool |

### 1.3 TUI input handler chain

`ai_coding/ui/textual_app.py:136-145`:

```python
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self._input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self.submit_slash(text)
        else:
            self._transcript.append_user(text, _clock())
            self._submit_text(text)
```

`ai_coding/ui/textual_app.py:156-182`:

```python
    def _submit_text(self, text: str) -> None:
        """Run it now, or queue it behind the turn that is already running."""
        if self._turn_active:
            self._enqueue(text)
        else:
            self._dispatch(text)
    ...
    def _dispatch(self, text: str) -> None:
        """Start the turn worker; its completion is drained by the state hook."""
        self._turn_active = True
        if text.startswith("/"):
            self.run_command(text)
        else:
            self.run_turn(text)
```

### 1.4 REPL path

`ai_coding/ui/repl.py:68-82`:

```python
    cmd = parse_slash(text)
    if cmd is None:
        stripped = text.strip()
        if stripped.startswith("/"):
            token = stripped.split(None, 1)[0].lower()
            suggestion = suggest_command(token)
            ...
        answer = await loop.process_input(session, text, on_delta=console.delta)
```

Slash dispatch is a long `if cmd.name == ...` chain (`repl.py:84-188`), returning `REPLAction.CONTINUE | QUIT | NEW_SESSION`.

### 1.5 Slash command path in the TUI

`/plan` and `/resume` are intercepted **before** the generic handler (`textual_app.py:214-226`):

```python
            if text == "/plan" or text.startswith("/plan "):
                await self._plan_turn(_plan_topic(text))
                return
            if text == "/resume":
                ...
                self._open_resume_picker()
                return
            console = TextualConsole(self._transcript)
            action, new_session = await handle_input(
                self._agent_loop, text, self._session, self._sessions, console
            )
```

A bare `/` opens the palette (`textual_app.py:115-121`).

### 1.6 Is there a queue?

**Two queues, both in-memory only.**

**(a) UI submission queue** — `textual_app.py:89-90`:

```python
        self._queued: deque[str] = deque()
        self._turn_active: bool = False
```

`textual_app.py:163-174`:

```python
    def _enqueue(self, text: str) -> None:
        # Bare /resume opens a modal picker and returns; its worker cannot
        # stand in for the whole resume, so it is refused rather than queued
        ...
        self._queued.append(text)
        self._transcript.append_block(Block("info", "[queue] 回合结束后执行"))
```

**(b) `SteeringQueue`** — `ai_coding/core/steering.py:27-30`:

```python
    def __init__(self, *, max_messages: int = 5, max_chars: int = 2_000) -> None:
        self._lines: deque[str] = deque()
```

Drained at the **iteration boundary**, `agent_loop.py:487-510`:

```python
    def _take_steering(self, session: SessionData, books: TurnBooks, index: int) -> None:
        """Absorb whatever the user typed, right before this request is assembled.
        ...
        """
        if self.steering is None:
            return
        lines = self.steering.drain()
```

FACT: the shipped TUI constructs a `SteeringQueue` (`textual_app.py:510`) and passes it to the loop (`textual_app.py:528`), but **no shipped code calls `.put()`**. A repo-wide grep for `.put(` under `ai_coding/` returns exactly one hit, `ai_coding/ui/transcript.py:405` (`fold.chunks.put(text)` — a UI chunk inbox, unrelated). Only tests and `scripts/r7e_cases.py` call `SteeringQueue.put`. So the steering channel has no producer in the shipped app.

### 1.7 Persistence of pending work

**No queue/inbox is persisted.** The session whitelist (`ai_coding/domain/session.py:215-246`) has no queue field; the background sub-agent table is explicitly in-process only — `ai_coding/core/subagent.py:76-79`:

```python
#: Background table cap (R1-2): a long-lived process must not grow the table
#: without bound.  Running rows are never pruned; the oldest terminal rows go
#: first.  Restarts lose the whole table anyway (documented, Hermes rule).
_MAX_BACKGROUND_ROWS = 200
```

What *is* persisted mid-turn is the **transcript**, via `ai_coding/core/turn_close.py:154-158`:

```python
    def checkpoint(self, session: SessionData, index: int) -> None:
        """Persist mid-turn, throttled once the session has grown large."""
        limit = int(self._settings.persist_checkpoint_messages)
        if len(session.messages) <= limit or index % LONG_SESSION_STRIDE == 3:
```

(exact: `index % LONG_SESSION_STRIDE == 0`, with `LONG_SESSION_STRIDE = 3` at `turn_close.py:49`, default `persist_checkpoint_messages: int = 200` at `config/models.py:114`.)

---

## 2. THE TURN LOOP

### 2.1 The main loop

`ai_coding/core/agent_loop.py:273-343` (the `while True` header through the NUDGE branch):

```python
        try:
            index = 0
            while True:
                index += 1
                budget_expired = frame.deadline is not None and frame.deadline.expired
                # R0-2: derive the open unknowns from the books before deciding.
                # One call site per round (here, not after every branch), so a
                # flat round counts exactly once.
                self._refresh_ledger(books)
                decision = self.policy.decide_next_iteration(
                    books.snapshot(
                        iteration=index, tokens_spent=self.reporter.tokens_spent(),
                        open_items=task.open_items(),
                        continuations=session.continuations,
                    ),
                    budget_expired=budget_expired,
                )
                if decision.action is TurnAction.FINISH:
                    ...
 break
                if decision.action is TurnAction.NUDGE:
                    ...
                    if restart:
                        index = 0
                    continue
```

The three moves per iteration (`agent_loop.py:345-422`): `books.note_iteration` → `_take_steering` → emit `IterationStarted` → `assembler.assemble(...)` → `_model_step(...)` → either the no-tool-calls branch (`376-406`) or `_run_tools(...)` (`408-422`).

### 2.2 The iteration unit

One iteration = **one decision + one model step + one tool batch**. Index is `1`-based and increments at the top (`agent_loop.py:274-276`). `index` is **reset to 0** by the C3 auto-continuation (`agent_loop.py:341-342`), so it is round-local; the books keep the cross-round total (`turn_books.py:151-160`):

```python
        self.iterations = index
        self.total_iterations += 1
```

### 2.3 All exit conditions and `TurnStatus` values

`ai_coding/domain/loop.py:16-26` — 7 members:

```python
class TurnStatus(enum.Enum):
    """How a turn terminated."""

    COMPLETED = "completed"  # model stopped and (if todo_aware) no open items
    MAX_ITERATIONS = "max_iterations"  # iteration budget exhausted
    TIMEOUT = "timeout"  # the wall-clock turn deadline cut the turn short (R2a)
    BUDGET = "budget"  # the turn's token ceiling was reached (R4b)
    STOPPED = "stopped"  # approval callback asked to stop
    ABORTED = "aborted"  # user interrupted (CancelledError)
    ERROR = "error"  # engine failed
```

Where each is produced:

- `COMPLETED` — `agent_loop.py:307-312` (post-budget acceptance verified) and `agent_loop.py:392-398` (model finished, acceptance verified).
- `MAX_ITERATIONS` / `TIMEOUT` / `BUDGET` — `turn_close.py:55-71`:

```python
    if budget_expired:
        status = TurnStatus.TIMEOUT
    elif decision.reason == "turn token budget exhausted":
        status = TurnStatus.BUDGET
    else:
        status = TurnStatus.MAX_ITERATIONS
```

- `STOPPED` — `turn_close.py:74-81`, reached from `agent_loop.py:420-422` when any outcome has `halted=True` (i.e. the approval callback returned STOP).
- `ABORTED` — `turn_close.py:84-88`, reached from the `asyncio.CancelledError` handler (`agent_loop.py:423-434`).
- `ERROR` / `TIMEOUT` — `turn_close.py:91-105` (`failed_outcome`), from `agent_loop.py:435-438` catching `(DeadlineExpired, EngineError)`.
- Pre-loop default before any body runs: `agent_loop.py:269-271`:

```python
        outcome = TurnOutcome(
            status=TurnStatus.MAX_ITERATIONS, reason="iteration budget exhausted"
        )
```

### 2.4 Default max iterations and where configured

`ai_coding/core/turn_policy.py:66`:

```python
    max_iterations: int = 10
```

Read from config at `turn_policy.py:96`:

```python
            max_iterations=max(1, int(getattr(settings, "max_tool_iterations", 10))),
```

`ai_coding/config/models.py:40`:

```python
    max_tool_iterations: int = 10
```

Emitted per iteration for the UI (`agent_loop.py:347-351`):

```python
                self._emit(
                    IterationStarted(
                        index=index, max_iterations=self.policy.config.max_iterations
                    )
                )
```

Plan steps use a *different* ceiling: `plan_step_max_iterations: int = 8` (`models.py:93`), read at `plan_runner.py:337-339`.

### 2.5 Interruption (Esc) — mechanism and propagation

**Layer1 — the batch-level fast path (`asyncio.Event`).** The event is created once and shared (`textual_app.py:518-520`):

```python
    # Built before the app so the loop and the app can share one event: the app
    # owns the Esc binding and sets it, the dispatcher waits on it.
    cancel = asyncio.Event()
```

The loop clears it before every batch so one key press drops one batch (`agent_loop.py:670-673`):

```python
        if self.cancel is not None:
            # R8c: one Escape drops the batch that is starting, not the whole turn.
            self.cancel.clear()
```

It reaches `run_with_deadline` (`dispatch.py:436-450`):

```python
        kill = threading.Event()
        ctx = replace(ctx, kill_event=kill)
        try:
            return await run_with_deadline(
                tool.execute(call.params, ctx),
                timeout=budget,
                label=f"tool:{call.name}",
                cancel=self.cancel,
            )
        except RunCancelled:
            kill.set()
            return self._cancelled(call)
```

`run_with_deadline` races the awaitable against `cancel.wait()` (`infra/deadline.py:163-180`):

```python
    watcher: asyncio.Task[bool] | None = None
    if cancel is not None:
        watcher = asyncio.ensure_future(cancel.wait())
        ...
        if watcher is not None and watcher in done:
            _abandon(task, on_abandon)
            raise RunCancelled(label)
```

The `kill_event` exists so a subprocess dies even though the awaiting task was abandoned (`dispatch.py:437`, `tools/base.py:53-56`). The resulting result is structured, not an exception (`dispatch.py:608-619`):

```python
            error=CANCELLED_BY_USER,
```

and it is explicitly excluded from the escalation ladder (`turn_books.py:284-285`):

```python
        counted = [item for item in outcomes if item.result.error != CANCELLED_BY_USER]
```

**Layer 2 — `CancelledError` at whatever await point.** `textual_app.py:361-382`:

```python
    def action_cancel_turn(self) -> None:
        """Abort the running turn (Esc).
        ...
        """
        self._cancel_event.set()
        for worker in self.workers:
            if worker.group != "turn" or worker.node is not self:
                continue
            if worker.is_cancelled:
                continue
            worker.cancel()
```

The loop handles it (`agent_loop.py:423-434`):

```python
        except asyncio.CancelledError:
            # Record what happened, persist the partial transcript, then let the
            # cancellation propagate — swallowing it is an asyncio anti-pattern.
            self._close_turn(
                session,
                user_input=user_input,
                task=task,
                frame=frame,
                books=books,
                outcome=aborted_outcome(books.last_text, books.total_iterations),
            )
            raise
```

UI rendering of the abort: `ai_coding/ui/blocks.py:292-304` renders `[turn] 已中断（Esc），任务未收尾` and explicitly refuses to promise resumption ("No '继续可接上' promise either").

---

## 3. TURN POLICY

Full read of `ai_coding/core/turn_policy.py`. Declared pure: *"Everything here is pure: no IO, no asyncio, no events, no persistence"* (`turn_policy.py:5-6`).

### 3.1 What it decides

Two questions (`turn_policy.py:9-12`): **may another iteration start** (`decide_next_iteration`, `:184-272`) and **how does a finished model step end** (`decide_after_step`, `:290-321`). Actions (`turn_policy.py:114-119`):

```python
class TurnAction(enum.Enum):
    ITERATE = "iterate"  # start another model step
    NUDGE = "nudge"  # inject ``decision.inject`` and iterate again
    FINISH = "finish"  # the turn is over
```

**Token budget?** Yes — as a *spend cap*, `turn_policy.py:193-195`:

```python
        token_gate = bool(self.config.max_turn_tokens) and (
            snapshot.tokens_spent >= self.config.max_turn_tokens
        )
```

**Context budget?** No. The policy never sees context size; context is the compactor's job (§4).

**Tool result persistence?** No. Persisting large tool outputs is `context_compression.persist_large_outputs` (§4). The policy owns only the prompt *text* for gates.

**Summarization?** No — `continuation_message`, `nudge_message`, `acceptance_message`, `ledger_message`, `strategy_message` (`:274-394`) are static templates. `NUDGE_ITEM_LIMIT = 10` caps each list (`:59`, applied at `:348`, `:357`, `:370`, `:391`).

`inject_task` (`:325-334`) decides **where** the task list lands:

```python
        if self.config.task_injection == "system":
            merged = f"{instructions}\n\n{render}" if instructions else render
            return TaskInjection(instructions=merged)
        block = ChatMessage(
            role="user", content=f"{TASK_MESSAGE_LEAD}\n{render}", timestamp=""
        )
```

Default is `"message"` (a volatile tail block), so the system prompt stays byte-identical.

### 3.2 The decision ladder (`decide_next_iteration`, in order)

1. **Clock wins** (`:188-189`): `if budget_expired: return TurnDecision(TurnAction.FINISH, "turn deadline exceeded")`
2. **Iteration cap** (`:196-224`): if over, either NUDGE `"continue unfinished work"` (C3; blocked items excluded, `:211-213`) or FINISH `"iteration budget exhausted"`.
3. **Token gate** (`:225-226`): FINISH `"turn token budget exhausted"`.
4. **Stall** (`:235-245`): FINISH `f"no progress: no tool call has succeeded in {snapshot.iteration - 1} iterations"`.
5. **Strategy nudge** (`:246-257`): NUDGE `"repeated tool failures"`.
6. **Ledger gate** (`:262-271`): NUDGE `"uncertainty ledger flat"` when `ledger_flat_rounds >= LEDGER_FLAT_ROUNDS`.
7. Otherwise ITERATE (`:272`).

Constants (`turn_policy.py:51-59`):

```python
ACCEPTANCE_GATE_LEAD = "[acceptance-gate]"
LEDGER_GATE_LEAD = "[ledger-gate]"
LEDGER_FLAT_ROUNDS = 2
NUDGE_ITEM_LIMIT = 10
```

`decide_after_step` (`:290-321`) — fail-closed acceptance (`:292-307`):

```python
            failed = [a for a in snapshot.acceptance if a.passed is not True]
            if not failed:
                return TurnDecision(TurnAction.FINISH, "acceptance verified")
            if snapshot.acceptance_nudges >= self.config.max_acceptance_nudges:
                return TurnDecision(
                    TurnAction.FINISH, "acceptance nudge budget exhausted"
                )
```

then todo gate (`:308-321`): `stop_policy == "natural"` finishes; `"strict"` never gives up early (`:310-316`).

### 3.3 Knobs and defaults

| `TurnPolicyConfig` field | Default (`turn_policy.py`) | Config key | Config default (`config/models.py`) |
|---|---|---|---|
| `max_iterations` | 10 (`:66`) | `ai.max_tool_iterations` | 10 (`:40`) |
| `stop_policy` | `"todo_aware"` (`:67`) | `ai.stop_policy` | `"todo_aware"` (`:59`) |
| `max_todo_nudges` | 2 (`:68`) | `ai.max_todo_nudges` | 2 (`:60`) |
| `task_injection` | `"message"` (`:69`) | `ai.task_injection` | `"message"` (`:67`) |
| `max_turn_tokens` | 0 (off) (`:72`) | `ai.turn_token_budget` | 0 (`:123`) |
| `failed_streak_threshold` | 2 (`:76`) | `ai.failed_streak_threshold` | 2 (`:138`) |
| `max_strategy_nudges` | 1 (`:77`) | `ai.max_strategy_nudges` | 1 (`:139`) |
| `stall_iterations` | 3 (`:78`) | `ai.stall_iterations` | 3 (`:140`) |
| `max_acceptance_nudges` | 2 (`:82`) | `ai.max_acceptance_nudges` | **absent from `AIConfig`** — falls to the `getattr` default at `turn_policy.py:106` |
| `max_ledger_nudges` | 1 (`:85`) | `ai.max_ledger_nudges` | **absent from `AIConfig`** — `turn_policy.py:107` default |
| `max_auto_continuations` | 3 (`:90`) | `ai.max_auto_continuations` | 3 (`:98`) |

Not in the policy, but read by the loop from the same `AIConfig`: `turn_timeout_seconds: float = 600.0` (`models.py:90`), `approval_timeout_seconds: float = 0.0` (`models.py:72`), `persist_checkpoint_messages: int = 200` (`models.py:114`), `tools_chars_warn: int = 12_000` (`models.py:83`), `working_memory_chars: int = 1200` / `working_memory_keep: int = 12` / `working_memory_note_chars: int = 120` (`models.py:128-134`).

---

## 4. CONTEXT ASSEMBLY

### 4.1 Order — declared, never sorted

`ai_coding/core/request_assembly.py:88-123`:

```python
        history = await self._compactor.compress(
            session.messages, deadline=deadline, on_summary=books.summaries.append
        )
        compacted = Compactor.did_compact(session.messages, history)
        instructions = (
            await self._build_instructions(deadline)
            if self._build_instructions is not None
            else None
        )
        injection = self._policy.inject_task(task, instructions)
        instructions = injection.instructions
        memory_block, memory_ids = self._memory.block()
        if memory_block and not books.memory_announced:
            self._emit(MemoryInjected(note_ids=memory_ids, chars=len(memory_block)))
            books.memory_announced = True
        digest_block = render_working_memory(
            session.turn_digests,
            current_turn=turn_number,
            max_chars=int(getattr(self._settings, "working_memory_chars", 1200)),
            compacted=compacted,
        )
        tail, tail_blocks = build_tail(
            [
                volatile_block("memory", memory_block),
                volatile_block("task", content_of(injection.message)),
                volatile_block("hook", hook_context),
                volatile_block("digest", digest_block),
            ]
        )
        request = [*history, *tail]
```

**Block order: `memory` → `task` → `hook` → `digest`.** Module docstring, `request_assembly.py:9-11`: *"**order is a contract** — memory / task / hook / digest, never sorted, never merged"*. Enforced in `core/request_builder.py:59-75` (*"Order is the caller's declaration order — never sorted, never merged"*).

### 4.2 Which are volatile

All four tail blocks. `core/request_builder.py:40-51`:

```python
def volatile_block(name: str, text: str | None) -> TailBlock | None:
    """A tail block, or ``None`` when there is nothing to inject.

    An empty block is not an empty block: it is a message the model would pay for
    on every iteration, so "nothing to say" must mean "not in the request" (R7a;
    moved here from the loop, where it was ``_volatile``).
    """
    if not text:
        return None
```

`build_tail` skips `None` (`request_builder.py:70-75`). They are **never persisted** — they exist only in the assembled request (`request_assembly.py:123`), which is not written to `session.messages`.

By contrast the **nudges ARE persisted** — `agent_loop.py:339`:

```python
                    session.add_message(user_message(decision.inject or "", now_utc()))
```

### 4.3 Where each block goes

| Block | Source | Destination |
|---|---|---|
| system prompt | `_build_instructions` → `prompt.build(project_instructions, skills_catalog)` (`agent_loop.py:825-834`) | `instructions`; "memory is never part of it" (`agent_loop.py:826`) |
| task | `TurnPolicy.inject_task` (`turn_policy.py:325-334`) | tail block `"task"` by default, or merged into `instructions` when `task_injection == "system"` |
| memory | `TurnMemory.block()` (`memory_turn.py:50-72`) | tail block `"memory"` — *"It goes in the request's *tail*, never the system prompt"* (`memory_turn.py:52-54`) |
| hook | `TurnHooks.context(session)` at turn start (`agent_loop.py:478`) | tail block `"hook"` |
| digest | `render_working_memory` (`domain/turn_digest.py:227`) | tail block `"digest"`, **conditional on compaction** |

Two conditional gates:
- memory announced once per **turn**, not per iteration (`request_assembly.py:106-108`, `turn_books.py:103`);
- digest block only when this request's history was actually rewritten — `Compactor.did_compact` is an *identity* test (`compaction.py:38-47`):

```python
        return result is not source
```

and `render_working_memory` returns `""` when `not compacted` (`turn_digest.py:242-243`).

### 4.4 Empty-block dropping

`volatile_block` returns `None` (above); `build_tail` omits it. `TurnMemory.block()` returns `("", ())` when memory is off or empty (`memory_turn.py:56-61`). `render_working_memory` returns `""` on no digests (`turn_digest.py:245-246`).

### 4.5 Snipping — there is no `core/snipping.py`

The file is `ai_coding/core/context_compression.py`. Pipeline order is stated as a hard invariant (`context_compression.py:7-9`):

```python
Pipeline order is a hard invariant — L3 persist -> L1 snip -> L2 placeholder ->
(L4 LLM summary) -> hard trim -> sanitize pairs.  Every stage operates on a shallow
copy and must never mutate the caller's list.
```

Executed in `compress` (`context_compression.py:342-371`), a copy: `out = list(messages)  # copy — never mutate the caller's history`.

**The numbers (all quoted from source):**

| Constant | Value | Where |
|---|---|---|
| `max_context_tokens` | `48_000` | `models.py:43`; read `context_compression.py:317` |
| `max_messages` (L1 trigger) | `400` | `models.py:46`; `context_compression.py:318` |
| `snip_keep_head` | `3` | `models.py:47`; `context_compression.py:319` |
| `snip_keep_tail` | `20` | `models.py:48`; `context_compression.py:320` |
| `SNIPPED_HEADER` | `"[snipped {n} messages]"` | `context_compression.py:24` |
| `keep_recent_tool_results` (L2) | `3` | `models.py:49`; `context_compression.py:321` |
| `per_result_persist_bytes` (L3) | `30_000` | `models.py:50`; `context_compression.py:329` |
| L3 trigger | `max_context_tokens // 2` | `context_compression.py:340`: `self._max_result_bytes = max(1, self._max_context_tokens // 2)` |
| `preview_len` (L3 marker) | `2000` | `context_compression.py:174` |
| `l2_window_chars` (per side) | `200` | `models.py:54`; `context_compression.py:327` |
| `l2_window_budget` (total) | `4_000` | `models.py:55`; `context_compression.py:328` |
| `_NOTE_ALLOWANCE` | `160` | `context_compression.py:32` |
| `ELIDED_NOTE` | `"[Previous: used {tool} · {chars} chars elided{where}]"` | `context_compression.py:28` |
| `ELIDED_DIR` | `"transcripts/elided"` | `context_compression.py:29` |
| `PERSISTED_OUTPUT_PREFIX` | `"<persisted-output"` | `context_compression.py:22` |
| `l4_keep_tail` | `6` | `models.py:56`; `context_compression.py:330` |
| `HARD_TRIM_KEEP_TAIL` | `20` | `context_compression.py:33` |
| `_L4_BREAKER_THRESHOLD` | `3` | `context_compression.py:34` |
| `_MIN_PAGE_MESSAGES` | `4` | `context_compression.py:35` |
| `compress_soft_ratio` | `0.8` | `models.py:119`; `context_compression.py:332` |
| `compress_target_ratio` | `0.5` | `models.py:120`; `context_compression.py:333` |

`snip_middle` itself (`context_compression.py:62-77`) replaces the invisible middle with one placeholder message.

Token estimation heuristic (`context_compression.py:38-47`):

```python
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return math.ceil(cjk / 2) + math.ceil(other / 4)
```

Policy layer (`context_compression.py:375-378`):

```python
        soft = int(self._max_context_tokens * self._compress_soft_ratio)
        return estimate_tokens(_joined_history_text(messages)) > soft
```

Budget-driven path wrapped in a deadline and degrading to raw history (`compaction.py:49-89`); explicit `/compact` **rewrites the session** (`compaction.py:91-115`, `agent_loop.py:804-816`).

---

## 5. THE TOOL LAYER

### 5.1 Registration

`ai_coding/tools/registry.py:19-25` — duplicates rejected:

```python
    def register(self, tool: BaseTool) -> None:
        """Register a tool. Raises ``ValueError`` if the name is already taken."""
        if tool.name in self._tools:
            raise ValueError(
                f"tool '{tool.name}' is already registered; refusing to overwrite"
            )
```

Lookup order is **name-sorted**, deliberately, for prompt-cache stability (`registry.py:31-38`, and `tools/schema.py:33-38`: *"a stable tool list keeps request bodies (and therefore server-side prompt caching) stable across turns"*).

The single assembly point is `ai_coding/tools/builder.py:86-169` (`_assemble_registry`), which also decides `include_subagent` (`:158-167`).

### 5.2 How the schema reaches the model

`ai_coding/tools/schema.py:17-29`:

```python
def tool_schema(tool: BaseTool) -> dict[str, Any]:
    """Return the ``{"type": "function", ...}`` schema for one tool."""
    raw = tool.params_json_schema
    params: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    params.setdefault("type", "object")
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": params,
        },
    }
```

Computed once in the loop constructor (`agent_loop.py:191`), passed per step (`agent_loop.py:761-767`):

```python
            step = await run_with_deadline(
                self.engine.step(
                    request,
                    tools=self._tool_schemas,
                    instructions=instructions,
                    on_delta=on_delta,
                ),
```

and finally into the HTTP kwargs (`ai/openai_engine.py:152-155`):

```python
        if tools:
            kwargs["tools"] = tools
```

### 5.3 Dispatch: parallel or sequential?

`ai_coding/core/dispatch.py:175-214`:

```python
        prepared = [
            self._prepare(tool_call_from_ref(ref), iteration=iteration) for ref in calls
        ]

        if self._can_run_parallel(prepared):
            semaphore = asyncio.Semaphore(self.max_parallel)

            async def run(item: _Prepared) -> DispatchOutcome:
                async with semaphore:
                    return await self._handle(item, ctx, deadline, view)

            return list(await asyncio.gather(*(run(item) for item in prepared)))

        outcomes: list[DispatchOutcome] = []
        halted = False
        for item in prepared:
            if halted:
                outcomes.append(
                    DispatchOutcome(
                        call=item.call, result=self._skipped(item.call), denied=True
                    )
                )
                continue
            outcome = await self._handle(item, ctx, deadline, view)
            outcomes.append(outcome)
            halted = outcome.halted
        return outcomes
```

**All-or-nothing** (`dispatch.py:224-231`):

```python
    def _can_run_parallel(self, prepared: list[_Prepared]) -> bool:
        """Concurrency requires *every* call to be an ALLOW-tier safe tool."""
        for item in prepared:
            if item.tool is None or not item.tool.parallel_safe:
                return False
            if _verdict(item.decision) is not PermissionDecision.ALLOW:
                return False
        return True
```

### 5.4 Concurrency bound — the number

`dispatch.py:159`: `self.max_parallel = max(1, int(max_parallel))`, default parameter `max_parallel: int = 4` (`dispatch.py:149`), supplied from `agent_loop.py:200-204`:

```python
        self.dispatcher = ToolDispatcher(
            tools,
            gate or PermissionGate(),
            approval,
            max_parallel=self.settings.max_concurrent_tools,
```

Config default: `max_concurrent_tools: int = 4` (`config/models.py:68`).

Two more bounds exist per call: `_call_budget` = `min(tool.max_duration_seconds, remaining turn budget)` (`dispatch.py:105-113`) and, on the outer side, the sub-agent semaphore (§9).

### 5.5 How `ToolContext` is built

`ai_coding/core/request_builder.py:78-103` builds the batch-shared context; the dataclass is `ai_coding/tools/base.py:27-56`:

```python
    task: TaskState = field(default_factory=TaskState)
    workspace: str = "."
    session_id: str = ""
    messages: tuple[ChatMessage, ...] = ()
    instructions: str | None = None
    emit: Callable[[Any], None] = _noop_emit
    call_id: str = ""
    turn: int = 0
    note_checkpoint: Callable[[str], None] | None = None
    kill_event: threading.Event | None = None
```

It is built per batch at `agent_loop.py:674-695`, then **the dispatcher stamps per-call identity** (`dispatch.py:245`):

```python
        ctx = replace(ctx, call_id=call.id)
```

and per-execution the kill flag (`dispatch.py:437-438`):

```python
        kill = threading.Event()
        ctx = replace(ctx, kill_event=kill)
```

`note_checkpoint` is the one channel a tool has into the turn's books (`agent_loop.py:684-687`); `checkpoint_write` is refused when it is absent (`tools/checkpoint_tool.py:71-80`).

### 5.6 Results become messages

`ai_coding/core/turn_close.py:134-152` — **one tool message per outcome, in outcome order**:

```python
    def write_back(
        self, session: SessionData, outcomes: Sequence[DispatchOutcome]
    ) -> None:
        """Append one tool message per outcome (the model's own words stay put)."""
        for item in outcomes:
            self.artifacts.extend(item.result.artifacts)
            session.add_message(
                tool_message(
                    ToolOutput(
                        call_id=item.call.id,
                        tool_name=item.call.name,
                        output=item.result.output,
                        ...
```

Strict pairing is re-enforced before every save (`turn_close.py:170`: `session.ensure_tool_pairing()`), and `dispatch.py:582-589` gives skipped-after-STOP calls a result so the declared tool_calls still pair up.

Every refusal mode is a **structured** `ToolResult`, not prose — nine factories at `dispatch.py:465-619`: `_unknown` (`unknown_tool`), `_denied` (`denied`), `_guard_denied` (`guard_denied`), `_hook_blocked` (`hook_blocked`), `_not_approved` (`not_approved`), `_approval_timeout` (`approval_timeout`), `_stopped` (`stopped`), `_skipped` (`skipped`), `_tool_timeout` (`tool_timeout`), `_cancelled` (`cancelled_by_user`).

### 5.7 Duplicate-call guarding — yes, two layers

**(a) Runtime guard** — `ai_coding/core/guard.py:145-182`:

```python
class DuplicateCallGuard:
    """Refuse a call that repeats one this turn has already made too often.
    ...
    """
    name = "duplicate_call"

    def __init__(self, threshold: int = 2) -> None:
```

fingerprint is order-insensitive/value-sensitive (`guard.py:72-79`):

```python
        payload = json.dumps(call.params, sort_keys=True, ensure_ascii=False, default=str)
    ...
    digest = hashlib.sha1(f"{call.name}:{payload}".encode()).hexdigest()
    return digest[:12]
```

Default threshold `duplicate_threshold: int = 2` (`models.py:162`), wired at `cli.py:211-213`, reset per turn at `agent_loop.py:474-475`:

```python
        self.dispatcher.guards.reset()
```

**(b) Registry-level name collision** — `registry.py:21-24`.

`GuardChain` fails closed on any raising guard (`guard.py:117-124`), and a `REWRITE` verdict must re-pass the static gate (`dispatch.py:296-307`).

---

## 6. THE PERMISSION LAYER

### 6.1 The three tiers

`ai_coding/security/permission_gate.py:19-22`:

```python
class PermissionDecision(enum.Enum):
    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"
```

### 6.2 Which tools are in which tier

**ALLOW** (`permission_gate.py:96-115`):

```python
        self._allow_tools = {
            "read",
            "glob",
            "todo_write",
            "skill",
            "subagent",
            "memory_search",  # read-only recall (R3b)
            "memory_write",
            "ask_user",
            "checkpoint_write",
        }
```

**WARN** (`permission_gate.py:116`):

```python
        self._warn_tools = {"write", "edit", "bash"}
```

**DENY** — no tool name; only bash command patterns (`permission_gate.py:70-81`):

```python
_BASH_HARD_DENY_PATTERNS: list[tuple[str, str]] = [
    ("rm -rf /", "recursive root deletion"),
    ("sudo ", "sudo escalation"),
    ("shutdown", "system shutdown"),
    ("reboot", "system reboot"),
    ("mkfs", "filesystem format"),
    ("dd if=", "disk write"),
    ("> /dev/sd", "raw disk write"),
    ("git push --force", "force git push"),
    ("git push -f ", "force git push (short)"),
    ("chmod 777", "world-writable permissions"),
]
```

Checked **first** (`permission_gate.py:122-134`).

**Trusted MCP servers → ALLOW** (`permission_gate.py:150-152`):

```python
        server, sep, _remote = tool_name.partition("__")
        if sep and server in self._trusted_servers:
            return Decision(PermissionDecision.ALLOW, "")
```

### 6.3 Default fail-closed behavior

Three distinct fail-closed points:

1. **Unknown tool → WARN**, never ALLOW (`permission_gate.py:154-158`):

```python
        # 5. Unknown tools → WARN (fail-closed / conservative)
        return Decision(
            PermissionDecision.WARN,
            f"unknown tool '{tool_name}' requires confirmation",
        )
```

2. **Un-parseable gate decision → WARN** (`dispatch.py:89-98`):

```python
def _verdict(decision: Any) -> PermissionDecision:
    """Normalize a gate decision into the enum (unknown input fails closed)."""
    ...
    try:
        return PermissionDecision(str(decision).lower())
    except ValueError:
        return PermissionDecision.WARN
```

3. **WARN with no approval callback → refuse** (`dispatch.py:329-331`):

```python
        if verdict is PermissionDecision.WARN:
            if self.approval is None:
                return self._not_approved(call, "no approval callback"), True, False
```

plus `approval.py:152-153` (`return False  # fail-closed without a callback`) and `approval.py:75-76, 81-82, 88` (`# default: no (fail-closed)`).

### 6.4 The approval callback contract

`ai_coding/security/approval.py:18-33`:

```python
class ApprovalResult(enum.Enum):
    YES = "yes"
    NO = "no"
    STOP = "stop"


class ApprovalCallback:
    """Abstract protocol for interactive approval prompts."""

    async def approve(
        self,
        tool_name: str,
        params: dict[str, Any],
        reason: str,
    ) -> ApprovalResult:
        raise NotImplementedError
```

Three implementations: `CLIApprovalCallback` (stdin or injected async provider, `:36-96`), `AutoApproveCallback` (always YES, `:99-114`), and the free function `approve_from_gate_decision` (`:121-156`).

The wait is bounded by `_ask_budget` = `min(approval_timeout, remaining turn budget)` (`dispatch.py:116-128`); default `approval_timeout_seconds: float = 0.0` (`models.py:72`) means "turn clock only". An unanswered prompt yields a *distinct* error code (`dispatch.py:549-571`, `error="approval_timeout"`).

### 6.5 How the TUI / REPL provide it

**TUI** — a modal plus a session-scoped "always" set (`textual_app.py:408-443`):

```python
    async def approval_provider(
        self, tool_name: str, params: dict[str, Any], reason: str
    ) -> str:
        """Ask through a modal; return the string ``approval.py`` parses.
        ...
        """
        if tool_name not in ALWAYS_EXCLUDED and tool_name in self._always_allow:
            return "y"

        screen = ApprovalScreen(tool_name=tool_name, params=params, reason=reason)
 ...
        answer: str = await self.push_screen_wait(screen)
```

`ALWAYS_EXCLUDED = frozenset({"bash"})` (`ui/modals.py:33`); the always-set is **in-memory only** (`textual_app.py:91-95`).

Wiring (`textual_app.py:517, 540`):

```python
    approval = CLIApprovalCallback()
    ...
    approval.set_provider(app.approval_provider)
```

Re-armed after every slash command, because `/permissions confirm` installs a fresh callback with no provider (`textual_app.py:332-344`).

**REPL / non-interactive** — `cli.py:88-91`:

```python
    if approval is None:
        from ai_coding.security.approval import AutoApproveCallback

        approval = AutoApproveCallback()
```

**Sub-agents** — `builder.py:53-55`: *"No human is present inside an isolated sub-agent to confirm WARN tier; the DENY tier (dangerous bash) still blocks regardless."*

---

## 7. THE MEMORY LAYER

### 7.1 When the constraint block is built

Per iteration, in the assembler (`request_assembly.py:105`):

```python
        memory_block, memory_ids = self._memory.block()
```

`ai_coding/core/memory_turn.py:50-72`:

```python
    def block(self) -> tuple[str, tuple[str, ...]]:
        """The volatile constraint block for this request: ``(text, note ids)``.

        It goes in the request's *tail*, never the system prompt, so a change
        costs only that block's cache (docs/r3-analysis.md 1.13).
        """
        if self._memory is None:
            return "", ()
        try:
            notes = self._memory.index_notes()
            if not notes:
                return "", ()
            return self._memory.render_index_block(notes), tuple(
                note.id for note in notes
            )
```

Selection and budget (`memory/service.py:522-560`): pinned notes and `pinned_kinds` (default `["decision"]`, `models.py:264`), sorted pinned-first/decision-first/newest-first, cut at `index_max_chars: int = 600` (`models.py:260`) minus `_INDEX_OVERHEAD`.

### 7.2 Where the block lands — tail, not system prompt

Three independent confirmations:
- `memory_turn.py:52-54` (above): *"the request's tail, never the system prompt"*
- `core/events.py:268-272`: *"Memory never enters the system prompt: it is a tail block, so a change costs only that block's cache"*
- `agent_loop.py:826`: `"""Assemble the **static** system prompt (memory is never part of it)."""`

Mechanically: tail block `"memory"`, first in order, dropped when empty (`request_assembly.py:117`, `request_builder.py:40-51`).

### 7.3 When harvest runs

Once per turn, in `_finish_turn`, **after** the close (`agent_loop.py:548-560`):

```python
        await self.turn_memory.harvest(
            session,
            deadline=frame.deadline,
            summaries=books.summaries,
            outcome=outcome,
            turn=frame.turn_number,
            evidence="tool" if books.evidence_calls else "model",
        )
```

`evidence` comes from `TurnBooks.evidence_calls`, which excludes state-only tools (`turn_books.py:52-55`):

```python
STATE_ONLY_TOOLS = frozenset({"checkpoint_write"})
```

### 7.4 Cursor semantics

`memory_turn.py:107-108`:

```python
        start = int(getattr(session, "harvested_messages", 0) or 0)
        delta = session.messages[start:]
```

Advanced only on success, *after* the harvest returns (`memory_turn.py:127-128`):

```python
            session.harvested_messages = len(session.messages)
            self._save(session)
```

Failure path returns before that (`memory_turn.py:146-153`), matching the module's stated invariant (`memory_turn.py:13-14`): *"a failed harvest **must not advance that cursor** — losing messages is worse than skipping a turn of extraction."*

The cursor lives on the session and **is** persisted (`domain/session.py:228-229`):

```python
        if self.harvested_messages:
            data["harvestedMessages"] = self.harvested_messages
```

because, per `session.py:112-115`, *"the CLI builds a fresh loop for every turn, while the session is reloaded each time."*

Harvest window and hints (`memory/service.py:597-609`): last `harvest_max_messages: int = 12` (`models.py:262`), plus a synthetic `<known-memory>` system hint (`:605-607`) and the compaction summary (`:608-609`). Message refs are computed **before** those synthetic appends (`:598-604`).

### 7.5 Consolidate gating

`memory_turn.py:154-155`:

```python
        if outcome.text is None:
            return  # nothing was concluded; no reason to wake consolidation
```

### 7.6 The extractor's role

Injected at composition (`cli.py:108-114` → `build_extractor`, `ai/llm_helpers.py:80-120`). One-shot OpenAI-compatible call, `max_tokens=400`, temperature `0.2`, `max_retries=0` (`llm_helpers.py:37-52`), truncated to `_MAX_EXTRACT_CHARS = 40_000` (`llm_helpers.py:27`). Returns structured `{text, kind, importance, action, target_id, scene}` JSON; `action ∈ add|supersede|update|noop` (`llm_helpers.py:123-136`). Never raises (`llm_helpers.py:117-118`).

Write gating (`memory/service.py:589-596`): `enabled`, `is_extraction_allowed(config, "harvest")`, `auto_extract`, extractor present. Without an extractor it still enforces the store cap: `self._store.evict(int(self._get("max_index_entries", 200)))`.

All memory calls are best-effort — every failure emits `Degraded` and returns (`memory_turn.py:65-72`, `:146-153`, `:174-180`).

---

## 8. THE EVENT SYSTEM

### 8.1 `LoopEvent` union — 21 members

`ai_coding/core/events.py:314-336`:

```python
LoopEvent = (
    IterationStarted
    | AssistantTextDelta
    | AssistantMessage
    | ToolApprovalRequested
    | ToolStarted
    | ToolFinished
    | ToolChunk
    | ModelStepFinished
    | ContextAssembled
    | Degraded
    | ToolSurfaceWarning
    | InputSteered
    | ScopedEvent
    | TaskUpdated
    | BackgroundTaskDone
    | LedgerUpdated
    | TurnFinished
    | MemoryInjected
    | MemoryRecalled
    | MemoryHarvested
    | MemoryConsolidated
)
```

All members are `@dataclass(frozen=True)` value types (`events.py:8-10`: *"Events are deliberately *values*... so they are immutable"*). Output previews are capped at `OUTPUT_PREVIEW_CHARS = 500` (`events.py:23`, `:369-375`).

### 8.2 Observer protocol

`events.py:339-349`:

```python
class LoopObserver(Protocol):
    """Anything that wants to watch a turn.  Sync — observers must not block."""

    def on_event(self, event: LoopEvent) -> None: ...


class NullObserver:
    """Observer that discards everything (the default when none is supplied)."""

    def on_event(self, event: LoopEvent) -> None:
        return None
```

Plus `RecordingObserver` for tests (`events.py:355-366`).

### 8.3 `Braid` / `SafeObserver` / `ScopedObserver` (+ `EmitObserver`)

`ai_coding/core/observers.py`:

- **`SafeObserver`** (`:51-71`) — wraps the one observer a turn talks to, swallows failures, **counts** them so the turn can report a `Degraded`:

```python
    def on_event(self, event: LoopEvent) -> None:
        try:
            self._inner.on_event(event)
        except Exception:  # noqa: BLE001 — UI failures must not kill the turn
            self.failures += 1
```

 The counter is consumed at `turn_report.py:180-185`.

- **`Braid`** (`:74-93`) — fan-out, each sink isolated:

```python
    def on_event(self, event: LoopEvent) -> None:
        for observer in self._observers:
            try:
                observer.on_event(event)
            except Exception:  # noqa: BLE001 — observation must never break the turn
```

  `len()` supported (`:92-93`). The loop builds `Braid(SafeObserver(base), SafeObserver(self.trace))` per turn (`agent_loop.py:619-626`).

- **`ScopedObserver`** (`:96-111`) — wraps a nested run's events with scope + parent:

```python
    def on_event(self, event: LoopEvent) -> None:
        self._inner.on_event(
            ScopedEvent(scope=self._scope, parent=self._parent, event=event)
        )
```

  Instantiated in `subagent.py:402-410` with `scope=f"subagent:{name}:{parent_span or 'root'}"`.

- **`EmitObserver`** (`:33-48`) — adapts a plain `emit` callable to the protocol, so `SubAgentTool` can fan child events into the parent stream via `ToolContext.emit` (`subagent_tool.py:262`, `:295`) without the tool layer importing observer machinery.

`observers.py:17`: *"All three are deliberately dumb: no filtering, no sampling, no mutation."*

### 8.4 What the trace writer persists

`ai_coding/core/trace.py` (625 lines):

- **`TurnWriter`** (`:200-241`) — append-only JSONL, hard byte cap, single `truncated` marker then refusal:

```python
    def write_line(self, line: str) -> bool:
        """Append one line.  Returns ``False`` if it was dropped."""
        if self._failed or self.truncated:
            return False
        if self._written + len(line.encode("utf-8")) + 1 > self.max_bytes:
            self.truncated = True
```

- **`TraceStore`** (`:244-358`) — layout `<root>/<session_id>/turn-<n>.jsonl`, turn numbers derived from the directory listing (`:274-282`), `summary.json` merged atomically via tmp+`os.replace` (`:319-337`), `prune()` by directory mtime (`:339-358`). Defaults `max_turn_bytes: int = 2_000_000`, `retention_days: int = 14` (`:251-252`, mirrored in `models.py:152-153`).
- **`TraceRecorder`** (`:375-625`) — a `LoopObserver`. Every payload passes through `Redactor` (`:85-109`, patterns at `:65-77`, `REDACTION_MARK = "«redacted»"`), previews trimmed to `_PREVIEW_LIMIT = 500` (`:361`). Spans assigned by `_describe` (`:596-625`): `turn` → `i<idx>` → `step<idx>` → `call_id`. Skips `ToolChunk` (`:511-512`).
- **`capture: full`** additionally snapshots the turn's messages to `turn-<n>.full.json` (`:441-466`).

### 8.5 Event-sourced, or just logged?

**Just logged.** Evidence:

- Nothing reads the JSONL back to reconstruct state. `TraceStore` consumers are: `aggregate.build_report` (reads `summary.json`, **not** the event log — `aggregate.py:107-130`), `repl._trace_report` → `ui/trace_render.py`, and `/budget`. `_describe` writes spans forward; no replay path exists.
- `summary.json` is a **separate projection written outside the event stream**, in `_finish_turn` (`trace.py:563-583`) by reading back and merging:

```python
        existing = self.store.read_summary(self.session_id)
        usage = self._turn_usage
        self.store.update_summary(
            self.session_id,
            {
                "turns": int(existing.get("turns", 0)) + 1,
                ...
```

  Two independent writes (a `.tmp`+replace), no shared transaction with the JSONL lines.
- Session state (`SessionData`) is authoritative and is **not** derived from events (`domain/session.py:215-295`).
- The docstring states the design constraint plainly (`trace.py:7-10`): *"it must be honest — a trace is only worth keeping if it says what really happened; it must be harmless — it may never change a request, a message or a decision, and it may never break a turn"*, and `agent_loop.py:32-33`: *"observation is **write-only** — it never changes a request, a message or a decision"*.

---

## 9. SUBAGENTS

### 9.1 How a child is created

`ai_coding/core/subagent.py:412-461` — a **fresh `AgentLoop` over the same class**:

```python
        registry = self._build_tools(workdir)
        engine = self._engine_factory()
        ...
        session = SessionData(
            session_id=f"sub-{uuid.uuid4().hex[:12]}",
            title=name,
            ...
            messages=seeded,
        )
        loop = AgentLoop(
            engine,
            _NullSessions(),  # type: ignore[arg-type]
            registry,
            approval=self._approval,
            prompt=prompt,
            settings=self._subagent_settings(),
            workspace=workdir or self._workspace,
            observer=self._scoped_observer(name, parent_span, observer),
        )
```

Sessions are swallowed — the ledger file is the child's persistence (`subagent.py:82-86`):

```python
class _NullSessions:
    """Swallows saves: a sub-agent's transcript is its ledger, not a session file."""

    def save(self, session: SessionData) -> None:
        return None
```

### 9.2 Isolation (worktree)

`subagent.py:186-212` — semaphore **before** the worktree, so the cap also bounds worktrees in flight (docstring `subagent.py:12-13`):

```python
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
                report_degraded(
                    self._scoped_observer(name, parent_span, observer),
                    "worktree_isolate",
                    "the worktree could not be created",
                    "ran in the main workspace instead",
                )
```

`WorktreeManager.temporary` = `git worktree add -b ai_sub_<name>_<token>` then forced remove in `finally` (`core/worktree.py:104-136`), branch prefix `_BRANCH_PREFIX = "ai_sub_"` (`:28`), base dir `<repo>/worktrees` (`:61-66`), every git call bounded by `_DEFAULT_GIT_TIMEOUT = 60.0` (`:30`, `:71-80`).

Enabled by `subagent_worktree_isolation: bool = True` (`models.py:42`), read at `builder.py:164`.

### 9.3 Depth limit

**Depth is bounded at 1 by construction, not by a counter.** `builder.py:39-42`:

```python
    def build_tools(workdir: str | None) -> Any:
        return build_tool_registry(
            app_cfg, workdir or main_ws, skills_path, include_subagent=False
        )
```

with the rationale at `builder.py:29-31`: *"a registry of tools — always without a nested `subAgent`, or delegation would recurse."* The child's registry has no `subAgent` and no `ask_user` (`builder.py:143-156`, gated on `include_subagent`). **A child cannot spawn children.**

### 9.4 Concurrency bound

`subagent.py:124-125`:

```python
        self._max_concurrent_n = max(1, int(max_concurrent))
        self._semaphore = asyncio.Semaphore(self._max_concurrent_n)
```

default `max_concurrent: int = 3` (`:99`), from `ai.max_concurrent_subagents: int = 3` (`models.py:41`) at `builder.py:50`. Taken in both paths: `run()` (`subagent.py:168`) and `_supervised` (`subagent.py:321`).

### 9.5 Foreground vs background

`SubAgentTool.execute` branches on the `background` param (`tools/subagent_tool.py:247-248`):

```python
        if params.get("background"):
            return self._spawn_background(task, name, context, parent, schema)
```

Foreground returns a full packet as a `ToolResult` (`subagent_tool.py:273`, `_to_tool_result` at `:82-115`). Background returns a handle immediately (`subagent_tool.py:309-316`):

```python
            data={"run_id": run_id, "background": True, "status": "running"},
```

The background table and live task refs are held on the runner (`subagent.py:129-131`), with a saturation guard (`subagent.py:244-249`):

```python
        if self._semaphore.locked():
            raise RuntimeError(
                "sub-agent concurrency exhausted: "
                f"{self._max_concurrent()} background slots busy; "
                "retry later or raise ai.max_concurrent_subagents"
            )
```

`add_done_callback(functools.partial(self._reap, run_id))` (`subagent.py:270`) closes the "cancelled before first step" hole (`:369-398`). `/tasks` reads the table via `_runner_of` (`repl.py:288-293`, `:296-331`).

### 9.6 How the child gets its tools

`BuildTools = Callable[[str | None], ToolRegistry]` (`subagent.py:51`), invoked as `registry = self._build_tools(workdir)` (`subagent.py:423`). `workdir=None` → main workspace, else the worktree path (`builder.py:37-42`). Context window mode (`subagent.py:138-141`):

```python
    @property
    def context_mode(self) -> str:
        """``full`` (the parent's view) or ``task_only`` (a clean window), R7e-4."""
        return str(getattr(self._settings, "subagent_context_mode", "full") or "full")
```

`full` seeds `list(parent.messages)` and a `PromptAssembler(parent.instructions)`; `task_only` seeds nothing and uses `base_instructions` (`subagent.py:430-444`). Default `"full"` (`models.py:79`).

Child settings are tightened (`subagent.py:494-509`): turn deadline `min(0.9 × self._timeout, configured)`, `max_auto_continuations: 0`. `_INNER_BUDGET_RATIO = 0.9` (`:70`).

### 9.7 How results return

`_packet` (`subagent.py:511-560`) maps `TurnStatus` → packet status via `_STATUS_MAP` (`:56-66`):

```python
    "budget": "max_iterations",
    "stopped": "error",
    "aborted": "error",
```

Conclusion truncated to `max_subagent_conclusion_chars: int = 4_000` (`models.py:107`), and the full packet is written to `transcripts/subagents/<hex12>.json` (`subagent.py:571-599`, `ELIDED`-style best effort). Schema facts are parsed from the **truncated** text (`subagent.py:536-543`).

### 9.8 Reasoning never returns

`subagent_tool.py:5-6`: *"The sub-agent's reasoning is not returned."* `ScopedEvent`'s docstring repeats it (`events.py:206-208`): *"The sub-agent's *reasoning* still never travels back (R1a-5); only these facts do."*

---

## 10. PLANNING

`ai_coding/core/plan_runner.py` (652 lines).

### 10.1 A plan is a todo list

`plan_runner.py:2-5`: *"A plan is a todo list; a run executes its open items in dependency order, one bounded turn each, with proof commands verified through the R0 gate that already lives in the loop."*

Design rationale for short sessions (`:9-11`):

```python
* One bounded turn per step, not one giant turn: chained short runs used
  ~30% fewer tokens than one-shot runs on the same work, because a scoped
  prompt explores less redundantly.
```

### 10.2 Each step is an ephemeral short session

`plan_runner.py:341-366`:

```python
        step = SessionData(
            session_id=f"plan-{uuid.uuid4().hex[:12]}",
            title=f"plan step {item.id}",
            created_time=now_utc(),
            last_access_time=now_utc(),
        )
        step.ephemeral = True
        # Shared, not copied: a step marking its item done moves the plan.
        step.task = task
        step.messages = [
            ChatMessage(role="user", content=self._seed(task, item, report), timestamp="")
        ]
```

`ephemeral` makes `TurnCloser.save` a no-op (`turn_close.py:168-169`) and keeps the trace on the parent session (`agent_loop.py:628-643`).

The seed is goal + open items only (`plan_runner.py:386-394`), with the quadratic-growth caveat documented at `:380-384`: *"the total seed text over an N-step plan is still quadratic in N (measured: 8 steps 2.4k chars, 16 → 8.8k, 32 → 34k, 64 → 132k)"*.

### 10.3 Bounded policy swap per step

`plan_runner.py:471-474`:

```python
        self._loop.policy = TurnPolicy(
            replace(original_policy.config, max_iterations=budget,
                    stop_policy="natural", max_auto_continuations=0)
        )
```

restored in `finally` (`:529-530`), budget = `plan_step_max_iterations` default 8 (`plan_runner.py:337-339`).

The planning turn gets the same treatment via `continuations_disabled` (`plan_runner.py:107-132`).

### 10.4 One repair turn per failing step

`plan_runner.py:495-504`:

```python
            if contracts and not self._contracts_passed(step_session):
                text = await self._loop.process_input(
                    step_session, self._repair_prompt(item, step_session),
                    on_delta=step_delta, observer=observer,
                )
                attempts = 2
```

Prompt at `:638-643`: *"上一步验收失败，只修复失败点，不要重做已通过的部分"*. Step prompt marker `STEP_HEAD = "只做这一步，不要做别的："` (`:42`).

### 10.5 Verdicts and run budget

`_settle` (`:572-610`) — mechanical verdicts win: verify-pass → `completed`, verify-fail → `blocked`, no-verify + model didn't claim done → `stalled` (retryable next run). Run ceiling `plan_max_step_turns: int = 20` (`models.py:105`, read at `:396-405`). Deadlock detection at `:226-231`.

### 10.6 Receipts on the parent

`_record_on_parent` (`:534-552`) writes a two-bubble marker; receipts in `session.step_deliveries`. One save at the end (`_persist`, `:257-269`). Stale receipts are pruned by step statement (`:272-290`).

---

## 11. THE UI LAYER AS ORCHESTRATOR

`ai_coding/ui/textual_app.py` (547 lines).

### 11.1 Input queue while a turn is active

Single-slot invariant + FIFO behind it. `:83-90`:

```python
        # Lines submitted while a turn is running.  The old guard against two
        # concurrent ``process_input`` calls was ``input.disabled = True`` for
        # whole turn — correct but it made the box unusable.  The guard is
        # the same (never two turns at once), it just queues the input instead
        # of dropping it.
        self._queued: deque[str] = deque()
        self._turn_active: bool = False
```

Enforcement is a Textual worker group: `@work(exclusive=True, group="turn", thread=False)` on `run_turn` (`:346`) and `run_command` (`:208`) — starting one cancels the group. Drained one item per completed turn (`:197-206`):

```python
    def _turn_done(self) -> None:
        self._turn_active = False
        if not self._queued:
            return
        nxt = self._queued.popleft()
        # Deferred, not called inline: starting a new exclusive worker cancels
        # every worker in the group, including this one still unwinding its
        # finally ...
        self.call_later(lambda: self._dispatch(nxt))
```

Terminal-state hook (`:184-195`) filters on group + node + state. `/resume` is **refused** while a turn runs (`:168-172`) rather than queued. The queue is unbounded — nothing caps `_queued`.

A second, separate in-memory inbox exists for streamed tool output, `SimpleQueue` per tool fold (`ui/transcript.py:61-74`, `fold.chunks.put(text)` at `:405`), because chunks arrive on subprocess reader threads. Throttled to `_CHUNK_THROTTLE_SECONDS = 0.15` per call (`ui/blocks.py:37`, `:394-402`).

### 11.2 EscCovered in §2.5. Binding at `:38`: `Binding("escape", "cancel_turn", "Cancel tool batch", show=False)`.

### 11.3 How background task completion is surfaced

Two surfaces, one event.

The **event** is emitted by the runner on the captured parent emit (`subagent.py:356-365`):

```python
        try:
            emit(BackgroundTaskDone(
                run_id=run_id, name=row.name, status=result.status,
                summary=summary, truncated=result.truncated,
                origin_session=origin_session,
                ledger_path=result.ledger_path,
            ))
```

summary capped at `_SUMMARY_TAIL = 200` (`subagent.py:74`, applied `:353-355`).

**In the TUI**, `TranscriptObserver` → `blocks_from_event` renders one dim line (`ui/blocks.py:333-345`):

```python
    if isinstance(event, BackgroundTaskDone):
        mark = "✓" if event.status == "completed" else "✗"
        line = f"{mark} 子任务完成 [{event.name}]（{event.status}）"
```

**Table**: `/tasks` reads `runner.task_list()` (`repl.py:296-331`).

FACT: the announcement works only if the run was spawned in the same process — the table is in-memory (§1.7). A child settling "possibly turns after the spawning turn ended" is the stated purpose (`events.py:226-228`), but it only reaches a live process's transcript.

### 11.4 Other orchestrator duties

- Stream flush timer: `self.set_interval(_FLUSH_SECONDS, self._flush_stream)` with `_FLUSH_SECONDS = 0.05` (`:27`, `:111`), so `on_delta` never touches the DOM directly (`:125-129`).
- Per-call transcript view (`begin_assistant` / `end_assistant`, `:350`, `:357`) — end runs in `finally`, replaying `step_deliveries` for `/resume`.
- Session switch rebuilds the view (`:287-299`).

---

## 12. WHAT IS ABSENT — yes/no with evidence

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Persistent event sourcing with projections in the same transaction? | **NO** | Trace is append-only JSONL via `TurnWriter.write_line` (`trace.py:224-241`); the `summary.json` projection is written by a **separate** read-merge-atomic-replace call (`trace.py:563-583`, `:319-337`). Nothing reads the event log to rebuild state: the only consumers are `aggregate.build_report` (reads `summary.json`, `aggregate.py:107-130`) and `ui/trace_render`. `agent_loop.py:32-33`: *"observation is **write-only** — it never changes a request, a message or a decision"*. Session truth is `SessionData.to_dict/from_dict` (`domain/session.py:215-295`), unrelated to events. |
| 2 | Session inbox/queue persisted to disk? | **NO** | UI queue: `self._queued: deque[str] = deque()` (`textual_app.py:89`), in-process only, drained one-per-turn (`:197-206`). Steering queue: `deque[str]` (`core/steering.py:28`), drained at the iteration boundary (`agent_loop.py:487-510`). Background table: in-process only, documented (`subagent.py:76-79`). Session whitelist has no queue key (`domain/session.py:215-246`). What is persisted mid-turn is the **transcript** (`turn_close.py:154-158`), not a queue. |
| 3 | Doorbell / wake pattern? | **NO** (with a partial analogue) | Zero hits for `doorbell`/`wake` in `ai_coding/`. The nearest analogue is `BackgroundTaskDone` (`events.py:222-244`), a best-effort in-process emit when a background child settles (`subagent.py:356-365`), rendered as one dim line (`ui/blocks.py:333-345`). There is no persisted notification, no pending-notification flag checked at turn start, and no wake-on-arrival queue. |
| 4 | LLM-summarized context compaction? | **YES** | L4 stage: `ContextManager._run_l4` (`context_compression.py:414-440`), gated by `_l4_should_run` (`:409-412`) with a 3-failure breaker (`:34`). Uses an injected summarizer (`context_compression.py:315`) built by `build_summarizer` (`ai/llm_helpers.py:58-77`) — a real `chat/completions` call, `max_tokens=800`, `temperature=0.2`. Head text capped at `_MAX_SUMMARY_CHARS = 60_000` (`llm_helpers.py:26`). Produces `[以下为早期对话摘要]` (`context_compression.py:437-440`); audit copy written to `transcripts/summary_<sha1>.txt` (`:442-449`). Degrades to the trimmed history on any failure (`:427-431`). **Separately**, the memory layer has an LLM extractor (see §7.6). |
| 5 | Mid-turn checkpoint / resume? | **PARTIAL — checkpoint yes, resume NO** | Checkpoint: `TurnCloser.checkpoint` persists mid-turn, throttled past200 messages by stride 3 (`turn_close.py:154-158`, `:49`; `models.py:114`), plus the model's own `checkpoint_write` note (`tools/checkpoint_tool.py`, `turn_books.py:253-262`). **No resume path**: `process_input` always restarts from `index = 0` (`agent_loop.py:274`), and `/resume` loads a whole *session* and replays it (`repl.py:246-285`, `textual_app.py:271-299`) — there is no entry that re-enters an in-flight turn at its iteration. Explicit about it: `ui/blocks.py:294-299` — *"No '继续可接上' promise either — resuming an aborted turn is a separate continuation-policy question."* |
| 6 | Model fallback chain on provider error? | **NO** | `agent_loop.py:435-438` catches `(DeadlineExpired, EngineError)` and produces `TurnOutcome(status=TurnStatus.ERROR)` via `failed_outcome` (`turn_close.py:91-105`); the loop returns and the turn is over. `EngineError` is raised, never retried, at `ai/openai_engine.py:211-214`; the SDK client is constructed with `max_retries=0` (`openai_engine.py:118`, and for helpers `llm_helpers.py:41`). Grep for `retry|backoff` returns no retry loop anywhere. `/model` switches the model for *future* turns (`agent_loop.py:237-249`, `repl.py:334-358`) — operator action, not automatic failover. |
| 7 | Per-agent tool allowlist? | **NO** | Every stack is built by one builder with a single boolean `include_subagent` (`builder.py:62-70`, `:86-92`). The child's stack is the parent's minus `subAgent` (`builder.py:39-42`). There is no per-agent name list, no per-agent permission gate (`PermissionGate()` is a bare default for both, `builder.py:110`, and the gate itself is a fixed 3-name table, `permission_gate.py:96-116`). The closest thing is a **negative** structural guarantee (no nesting) plus one config-derived allowlist for MCP servers: `mcp.servers[].tools` (`models.py:287`) and `auto_approve` (`models.py:290`, read at `permission_gate.py:160-175`) — server-level, not agent-level. |

---

## 13. SIZE MAP

Line counts (`wc -l`, exact):

| File | Lines | Absolute path |
|---|---|---|
| `core/agent_loop.py` | **856** | `/home/linsir365/projects/ai-coding/ai_coding/core/agent_loop.py` |
| `core/turn_policy.py` | **394** | `/home/linsir365/projects/ai-coding/ai_coding/core/turn_policy.py` |
| `core/request_assembly.py` | **137** | `/home/linsir365/projects/ai-coding/ai_coding/core/request_assembly.py` |
| `core/dispatch.py` | **622** | `/home/linsir365/projects/ai-coding/ai_coding/core/dispatch.py` |
| `core/memory_turn.py` | **186** | `/home/linsir365/projects/ai-coding/ai_coding/core/memory_turn.py` |
| `core/subagent.py` | **619** | `/home/linsir365/projects/ai-coding/ai_coding/core/subagent.py` |
| `core/trace.py` | **625** | `/home/linsir365/projects/ai-coding/ai_coding/core/trace.py` |
| `core/events.py` | **404** | `/home/linsir365/projects/ai-coding/ai_coding/core/events.py` |
| `core/observers.py` | **111** | `/home/linsir365/projects/ai-coding/ai_coding/core/observers.py` |
| `ui/textual_app.py` | **547** | `/home/linsir365/projects/ai-coding/ai_coding/ui/textual_app.py` |
| `security/permission_gate.py` | **178** | `/home/linsir365/projects/ai-coding/ai_coding/security/permission_gate.py` |

Adjacent orchestration files, same measure:

| File | Lines | Path |
|---|---|---|
| `core/plan_runner.py` | 652 | `ai_coding/core/plan_runner.py` |
| `core/turn_books.py` | 332 | `ai_coding/core/turn_books.py` |
| `core/turn_close.py` | 252 | `ai_coding/core/turn_close.py` |
| `core/context_compression.py` | 468 | `ai_coding/core/context_compression.py` |
| `core/compaction.py` | 121 | `ai_coding/core/compaction.py` |
| `core/guard.py` | 295 | `ai_coding/core/guard.py` |
| `core/steering.py` | 56 | `ai_coding/core/steering.py` |
| `core/request_builder.py` | 103 | `ai_coding/core/request_builder.py` |
| `core/turn_report.py` | 192 | `ai_coding/core/turn_report.py` |
| `core/turn_hooks.py` | 72 | `ai_coding/core/turn_hooks.py` |
| `core/aggregate.py` | 130 | `ai_coding/core/aggregate.py` |
| `core/worktree.py` | 139 | `ai_coding/core/worktree.py` |
| `tools/subagent_tool.py` | 324 | `ai_coding/tools/subagent_tool.py` |
| `tools/registry.py` | 41 | `ai_coding/tools/registry.py` |
| `tools/base.py` | 126 | `ai_coding/tools/base.py` |
| `tools/schema.py` | 47 | `ai_coding/tools/schema.py` |
| `tools/builder.py` | 172 | `ai_coding/tools/builder.py` |
| `security/approval.py` | 165 | `ai_coding/security/approval.py` |
| `security/user_prompt.py` | 101 | `ai_coding/security/user_prompt.py` |
| `ui/repl.py` | 425 | `ai_coding/ui/repl.py` |
| `domain/loop.py` | 66 | `ai_coding/domain/loop.py` |
| `domain/session.py` | 299 | `ai_coding/domain/session.py` |
| `domain/ledger.py` | 147 | `ai_coding/domain/ledger.py` |
| `domain/turn_digest.py` | 323 | `ai_coding/domain/turn_digest.py` |
| `infra/deadline.py` | 198 | `ai_coding/infra/deadline.py` |
| `ai/openai_engine.py` | 230 | `ai_coding/ai/openai_engine.py` |
| `ai/llm_helpers.py` | 158 | `ai_coding/ai/llm_helpers.py` |
| `memory/service.py` | 964 | `ai_coding/memory/service.py` |
| `config/models.py` | 345 | `ai_coding/config/models.py` |
| `cli.py` | 306 | `ai_coding/cli.py` |
| **package total** | **21,944** | `ai_coding/**/*.py` |