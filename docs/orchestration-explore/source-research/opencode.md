I have enough source. Here is the fact-only report.

---

# OpenCode Orchestration Layer — Source Facts

All paths are absolute under `/home/linsir365/projects/opencode`. The orchestration code lives in `packages/core/src/session/**`, `packages/core/src/{bus,tool,permission,instructions,instruction-discovery,snapshot,tool-output}.ts`, with provider/event primitives in `packages/ai/src` and `packages/schema/src`.

---

## 1. THE LOOP SHAPE

### The outer loop (iteration unit = one *step*, one *turn* = drain)

`/home/linsir365/projects/opencode/packages/core/src/session/runner/llm.ts:54-196`

```ts
54:    const drain = Effect.fn("SessionRunner.drain")(function* (input: Parameters<Interface["drain"]>[0]) {
55:      const sessionID = input.sessionID
56:      let force = input.force
57:      let continuing = input.continuation !== undefined
58:      let step = input.continuation?.step ?? 1
59:      let entering = true
```

The drain loop itself, `llm.ts:188-195`:

```ts
188:      while (true) {
189:        const next = yield* advanceToStep()
190:        if (next._tag !== "Ready") return next
191:        continuing = yield* runStep(next.context, step)
192:        step++
193:        force = false
194:        entering = false
195:      }
```

`advanceToStep` is an unbounded `while (true)` that blocks on the inbox (`llm.ts:71-186`); it either returns a `DrainResult` (terminal for the drain) or `{ _tag: "Ready", context }`.

**Drain termination union** — `/home/linsir365/projects/opencode/packages/core/src/session/runner/index.ts:23-28`:

```ts
23: export type DrainResult = Data.TaggedEnum<{
24:   Complete: {}
25:   Moved: { readonly continuation?: Continuation }
26:   Reloaded: { readonly force: boolean; readonly continuation?: Continuation }
27: }>
28: export const DrainResult = Data.taggedEnum<DrainResult>()
```

Produced in `llm.ts`: `Complete()` at `:63` and `:162`; `Moved({continuation})` at `:111`; `Reloaded({force, continuation})` at `:77`. `SessionExecution.drain` (`/home/linsir365/projects/opencode/packages/core/src/session/execution.ts:105-109`) folds `Moved`/`Reloaded` into a recursive re-drain and `Complete` into `Effect.void`.

### The inner loop (iteration unit = one *attempt*, `runStep`)

`llm.ts:206-297`. `runStep` returns `boolean | undefined` — `Completed.needsContinuation`:

```ts
270:        const completed = yield* SessionStep.Outcome.$match(outcome, {
271:          Completed: (outcome) => Effect.succeed(outcome.needsContinuation),
```

`undefined` means "loop again inside runStep"; a boolean means "the drain's next `advanceToStep` decides whether another step follows". `initial` is prepared once and reused only for the first attempt (`llm.ts:210, 214-216`).

### Step limit

`llm.ts:223`, `:237-241`:

```ts
223:        const stepLimitReached = loaded.agent.info.steps !== undefined && step >= loaded.agent.info.steps
...
237:          messages: stepLimitReached
238:            ? [...transcript.messages, Message.assistant(MAX_STEPS_PROMPT)]
239:            : transcript.messages,
240:          // Keep tool definitions on the final Step to preserve the provider's cached prefix.
241:          toolChoice: stepLimitReached ? "none" : undefined,
```

At step limit, `step.ts:88-90` rejects any tool call outright:

```ts
88:    const executeTool = (call: ToolCall) => {
89:      if (input.prepared.request.toolChoice?.type === "none")
90:        return new Tool.Error({ message: "Tools are disabled after the maximum agent steps" })
```

---

## 2. THE QUEUE / INBOX

### Item shape

`/home/linsir365/projects/opencode/packages/schema/src/session-inbox.ts:11-50`:

```ts
11: export const Delivery = Schema.Literals(["steer", "queue"])
...
37: const UserItem = Schema.Struct({ type: Schema.tag("user"), payload: UserPayload, delivery: Delivery })
38: const SyntheticItem = Schema.Struct({ type: Schema.tag("synthetic"), payload: SyntheticPayload, delivery: Delivery })
39: const CompactionItem = Schema.Struct({
40:   type: Schema.tag("compaction"),
...
44: const MoveItem = Schema.Struct({ type: Schema.tag("move"), payload: MovePayload, delivery: Delivery })
```

`UserPayload` = `Prompt.fields` (`text`, `files`, `agents`, `skills`) + `metadata` (`session-inbox.ts:15-18`); `Prompt` itself is `packages/schema/src/prompt.ts:65-70`.

### Enqueue

`/home/linsir365/projects/opencode/packages/core/src/session/inbox.ts:169-202`:

```ts
176:    const admitted = yield* bus
177:      .publish(SessionEvent.InboxEnqueued, {
178:        inboxID: request.id,
179:        sessionID: request.sessionID,
180:        item: request.item,
181:      })
```

Projection inserts the row in the same transaction: `inbox.ts:294-316` with `.onConflictDoNothing()` (`:312`). Table `session_inbox` at `/home/linsir365/projects/opencode/packages/core/src/session/sql.ts:125-145` — `enqueued_seq` is the durable event seq, with `uniqueIndex("session_inbox_session_enqueued_seq_idx")`.

### steer vs queue delivery semantics

Promotion scope — `inbox.ts:44-48`:

```ts
44: /**
45:  * Which pending input `promote` may consume: "steer" promotes steers only (a step
46:  * boundary mid-work), while "input" also allows one queued input when no steers are
47:  * waiting (the idle boundary, where the Session picks up fresh work).
48:  */
```

`inbox.ts:496-531` (`promote`): steers first (all of them, in `enqueued_seq` order), controls (`compaction` / `move`) checked *before* user items, then **at most one** queued input:

```ts
505:      const steers = yield* pendingSteers(db, sessionID)
506:      if (steers.length > 0 || scope === "steer") {
507:        const control = steers.findIndex((row) => row.type === "compaction" || row.type === "move")
508:        if (control === 0) return undefined
509:        return yield* publish(db, bus, sessionID, control === -1 ? steers : steers.slice(0, control))
510:      }
511:
512:      const queued = yield* db
...
518:        .limit(1)
...
520:      if (!queued) return 0
521:      if (queued.type === "compaction" || queued.type === "move") return undefined
522:      const promoted = yield* publish(db, bus, sessionID, [queued])
```

**Steer overtakes** because `pendingSteers` / the queued query both `orderBy(asc(enqueued_seq))` but steers are selected unconditionally first, and `limit(1)` applies only to the queue branch. `nextPromotable` (`inbox.ts:407-424`) applies the same precedence. The runner picks the scope from loop position — `llm.ts:80-87` and `:161`, `:170`:

```ts
83:                  const next = yield* SessionInbox.nextPromotable(
84:                    db,
85:                    sessionID,
86:                    entering || !continuing ? "input" : "steer",
87:                  )
...
161:              if (!force && !continuing && (!pending || (pending.delivery === "queue" && promotable === "steer")))
162:                return DrainResult.Complete()
...
170:                    entering && !continuing ? promotable : "steer",
```

Switching an item's delivery mode is a projection-guarded UPDATE, not a delete+insert: `inbox.ts:357-384` (`projectDelivery`, `where(... eq(delivery, input.from))`), with `steer`/`queue` at `inbox.ts:240-260`.

### Idempotent re-enqueue of the same message id

`reconcile` (`inbox.ts:155-167`) is called first inside `admit`:

```ts
161:    const existing =
162:      (yield* find(db, request.id)) ?? (yield* promotedFromMessage(db, request.sessionID, request.id, request.delivery))
163:    if (existing === undefined) return undefined
164:    if (existing.type === "compaction" || !matches(existing, request))
165:      return yield* new LifecycleConflict({ id: request.id })
166:    return existing
```

`promotedFromMessage` (`inbox.ts:113-143`) also accepts an id that has *already been delivered* (i.e. exists in `session_message`), which makes a retried prompt a no-op. `Session.prompt` uses this path first: `/home/linsir365/projects/opencode/packages/core/src/session/session.ts:150-157`. A concurrent duplicate that loses the `onConflictDoNothing` insert is recovered in `admit` via `Effect.catchDefect` → `find(db, request.id)` (`inbox.ts:191-197`). The survivor of the race re-checks `matches` and fails with `LifecycleConflict` otherwise (`inbox.ts:199-200`).

### Prompt returns immediately

`session.ts:174` — `if (input.resume !== false) yield* execution.wake(sessionID)`; `wake` is fire-and-forget (`/home/linsir365/projects/opencode/packages/core/src/session/execution.ts:172` → `coordinator.wake`, `run-coordinator.ts:136-145`, a bare `Effect.sync` that only mutates a map and maybe forks a fiber). `Session.prompt` returns the admitted `Info` immediately after that.

---

## 3. THE RUN COORDINATOR / DOORBELL

`/home/linsir365/projects/opencode/packages/core/src/session/run-coordinator.ts`.

State — `:40-47`:

```ts
40: type Execution<E, Reason> = {
41:   readonly done: Deferred.Deferred<void, E>
42:   owner?: Fiber.Fiber<void>
43:   scope: Promotable
44:   pendingWake?: Promotable
45:   stopping: boolean
46:   interruptionReason?: Reason
47: }
```

The `active` set is `executions` (`executions.set(key, execution)` at `:93`, `executions.delete(key)` at `:118`; exposed at `:179`).

**Doorbell (门铃) = `pendingWake`.** Ringing while active, `:136-145`:

```ts
136:    const wake = (key: Key, scope: Promotable = "input") =>
137:      Effect.sync(() => {
138:        const execution = executions.get(key)
139:        if (execution !== undefined) {
140:          // Coalesced wakes keep the widest scope: "input" subsumes "steer".
141:          execution.pendingWake = execution.pendingWake === "input" ? "input" : scope
142:          return
143:        }
144:        start(key, false, scope)
145:      })
```

Merging = one nullable slot holding the widest scope; `execution.scope` is overwritten by the pending one before the next drain (`:79`). The gap the doorbell exists to close is documented at `:32-39` and `:114-115`. The drain loop re-runs instead of ending while a wake is pending (`:74-85`):

```ts
77:          Effect.suspend(() => {
78:            if (execution.stopping || execution.pendingWake === undefined) return Effect.void
79:            execution.scope = execution.pendingWake
80:            execution.pendingWake = undefined
81:            // Trampoline so drains that complete synchronously cannot grow the stack.
82:            return Effect.yieldNow.pipe(Effect.andThen(loop(key, execution, false)))
83:          }),
```

A doorbell rung after the loop decided to end (or during cleanup) starts a *successor* execution in `settle` (`:116-120`):

```ts
117:      if (execution.pendingWake) start(key, false, execution.pendingWake)
118:      else executions.delete(key)
119:      Deferred.doneUnsafe(execution.done, exit)
```

`run` (join-or-start) — `:124-134`; a stopping execution refuses joiners and chains into a fresh run (`:129-131`).

### Interrupt / cancel of an in-flight turn

`run-coordinator.ts:147-167`:

```ts
150:        if (execution === undefined || execution.stopping) return false
151:        if (execution.owner === undefined) {
...
155:          execution.pendingWake = undefined
156:          return false
157:        }
158:        execution.stopping = true
159:        // Wakes recorded so far belong to the interrupted intent; the interrupt claims them.
160:        // Wakes arriving during cleanup are new admissions and restart normally at settle.
161:        execution.pendingWake = undefined
162:        execution.interruptionReason = reason
163:        // Fire and forget: nobody benefits from waiting out cleanup here...
165:        fork(Fiber.interrupt(execution.owner))
166:        return true
```

`awaitSettlement` is opt-in (`:183-193`). Wrapped as `SessionExecution.interrupt` with optional resume (`/home/linsir365/projects/opencode/packages/core/src/session/execution.ts:153-170`): after the interrupt is *accepted* it re-reads the inbox and re-wakes with `"steer"` if the next promotable item is a steer, compaction, or move (`:165-168`).

Inside the step, cancellation is scoped deliberately: `step.ts:136-144` restores the provider stream as interruptible, detects `streamInterrupted`, then interrupts all tool fibers (`:87`, `:142`, `:144`), and only the *terminal* publications run uninterruptibly:

```ts
135:    // Keep the final tool and Step events uninterruptible, even when the work itself is cancelled.
```

The exit maps to a terminal event in `settled` — `execution.ts:119-147`, with `terminal()` at `:50-54` (`succeeded` / `interrupted` / `failed`). Shutdown interruption deliberately **keeps** the write-ahead claim; deliberate stops release it (`execution.ts:128-136`).

---

## 4. CONTEXT ASSEMBLY

### Two phases: `select` + `load`

`/home/linsir365/projects/opencode/packages/core/src/session/context.ts:45-69`:

```ts
45: /**
46:  * Resolves model-request state in two phases: `select` fixes the Session,
47:  * agent, instruction sources, and tool snapshot; `load` adds the model and
48:  * active history for that selection. ...
51: export interface Interface {
52:   /** Selects the Session, agent, instructions, and tools used by subsequent work. */
53:   readonly select: (sessionID: SessionSchema.ID) => Effect.Effect<Selection, AgentNotFoundError>
54:   /** Resolves the model and active history for that selection. */
55:   readonly load: (selection: Selection) => Effect.Effect<Loaded, SessionRunnerModel.Error>
```

Returns: `Selection = { session, agent, instructions, tools }` (`context.ts:29-34`); `Loaded = { session, agent, model, initial, messages, tools }` (`context.ts:36-43`).

`select` (`context.ts:121-161`) — permissions merged first, then a parallel fan-out:

```ts
131:      const permissions = Permission.merge(agent.info.permissions, session.permissions ?? [])
132:      const loaded = yield* Effect.all(
133:        {
134:          tools: registry.snapshot(permissions),
135:          builtins: builtins.load(),
136:          discovery: discovery.load(),
137:          skills: skillInstructions.load(permissions),
138:          references: referenceInstructions.load(),
139:          mcp: mcpInstructions.load(permissions),
140:          entries: entries.load(sessionID),
141:        },
142:        { concurrency: "unbounded" },
143:      )
```

`load` (`context.ts:163-179`) adds `resolveModel(session)` + `SessionHistory.entriesForRunner(...)` split into `{ initial, entries }` (`history.ts:118-135`). The runner wraps `select` with `InstructionState.prepare` before `load` — `llm.ts:198-203`, `llm.ts:165`, `llm.ts:179`.

### System prompt: how many sources, in what order

`select` combines **7 ordered groups** (`context.ts:147-158`):

```ts
147:        // Ordered from most to least shared across sessions so the baseline stays a reusable
148:        // prompt-cache prefix: user-level catalog and guidance, then project instructions, then
149:        // the date and environment, which vary by day and directory.
150:        instructions: Instructions.combine([
151:          CodeModeInstructions.make(loaded.tools.codeModeCatalog),
152:          loaded.mcp,
153:          loaded.references,
154:          loaded.skills,
155:          loaded.discovery,
156:          loaded.builtins,
157:          loaded.entries,
158:        ]),
```

Concrete keys: `core/codemode` (`codemode/instructions.ts:126`), `core/mcp-guidance` (`mcp/instructions.ts:72`), `core/reference-guidance` (`reference/instructions.ts:78`), `core/skill-guidance` (`skill/instructions.ts:82`), `core/instructions` (`instruction-discovery.ts:17`), `core/date` + `core/environment` (`instructions/builtins.ts:25,34`), and one `api/<key>` per session entry (`session/instruction-entry.ts:92`).

The wire system array is **2 parts** — `/home/linsir365/projects/opencode/packages/core/src/session/model-request.ts:110-117`:

```ts
110:    system: [
111:      input.agent.system
112:        ? input.agent.system
113:        : SessionSystemPrompt.make(input.tools.definitions.map((tool) => tool.name)),
114:      input.initial,
115:    ]
116:      .filter((part) => part.length > 0)
117:      .map(SystemPart.make),
```

`initial` is the epoch baseline rendered by `InstructionState.initial` → `Instructions.renderInitial` (`session/instruction-state.ts:165-174`, `instructions/index.ts:161-169`). Default `system.txt` is the harness prompt with `${OPENCODE_TOOL_GUIDANCE}` substituted from the actual tool-name set (`session/system-prompt.ts:5-27`).

### History: bounded by compaction, not by a token cap

`/home/linsir365/projects/opencode/packages/core/src/session/history.ts:28-108`. `latestCompaction` picks the newest `status = 'completed'` compaction row bounded by a `Boundary` (`"latest" | "local" | provenance`), then `messageEntries` reads `seq >= compaction.seq`:

```ts
90:          compaction ? gte(SessionMessageTable.seq, compaction.seq) : undefined,
```

Nothing is truncated by a message/token count. Two *content* reductions happen at request build: `unsupportedParts` (`model-request.ts:169-170`) and `boundImages` with `IMAGE_BYTES_TRIGGER = 25 * 1024 * 1024` / `IMAGE_BYTES_TARGET = 15 * 1024 * 1024` (`model-request.ts:43-44`, `:172-199`).

### Compaction trigger and `keep`

`/home/linsir365/projects/opencode/packages/core/src/session/compaction.ts:189-197`:

```ts
189:    const state = State.create<Settings, Editor>({
190:      name: "session-compaction",
191:      initial: () => ({ auto: true, keep: 15_000 }),
```

`keep: 15_000` — "Tokens of recent conversation kept verbatim beside the summary" (`compaction.ts:42-43`). Trigger, `:199-213`:

```ts
204:      if (trigger.reason !== "manual" && !settings.auto) return { status: "skipped" }
205:      const ceiling = calculateCeiling(context.model.limit, settings.buffer)
206:      if (trigger.reason === "auto" && !due(context, ceiling)) return { status: "skipped" }
...
212:      const budget =
213:        trigger.reason === "overflow" ? Math.min(cap, Math.floor(estimateContext(context) * SHRINK_STEPS[0])) : cap
```

`due` (`:227-236`) skips if the last message is a just-completed compaction, or if the newest measured-prompt assistant is older than the newest native checkpoint, otherwise `estimateContext(context) >= ceiling`. `SHRINK_STEPS = [0.7, 0.5, 0.35]` (`:92`). Ceiling (`:890-897`):

```ts
893:  const window = limit.input || limit.context
894:  if (window <= 0) return Number.POSITIVE_INFINITY
895:  if (buffer !== undefined) return window - buffer
896:  return window - Math.max(Math.floor(window * 0.1), window >= 2 * RESERVE_MIN ? RESERVE_MIN : 0)
```

with `RESERVE_MIN = 16_000` (`:94`). Call sites: auto per step (`llm.ts:217`), overflow recovery (`llm.ts:262-268`), manual inbox item (`llm.ts:124-135`).

---

## 5. THE TOOL LAYER

### Registration (transform + editor)

`/home/linsir365/projects/opencode/packages/core/src/tool.ts:26-33`:

```ts
26: export interface Editor {
27:   readonly list: () => readonly (Tool.Info & { readonly id: string })[]
28:   readonly get: (id: string) => (Tool.Info & { readonly id: string }) | undefined
29:   readonly namespace: (namespace: Tool.Namespace) => void
30:   readonly add: (tool: Tool.Info) => void
31:   readonly update: (id: string, update: (tool: Types.Mutable<Tool.Info>) => void) => void
32:   readonly remove: (id: string) => void
33: }
```

Backed by `State.create` (`tool.ts:158-219`), which rebuilds from all registered transforms on read (`state.ts:172-194`). Plugins register through `ctx.tool.transform`, e.g. `/home/linsir365/projects/opencode/packages/core/src/tool/plugin/read.ts:38-46`:

```ts
38:    yield* ctx.tool
39:      .transform((editor) =>
40:        editor.add({
41:          name,
42:          options: { codemode: false },
```

The built-in tool plugins are listed at `/home/linsir365/projects/opencode/packages/core/src/plugin/internal.ts:213-247`. Registration is validated before insertion (`tool.ts:177-185`, `tool.ts:304-326`) — name regex, reserved `execute`, and a full `ToolDefinition.make` round-trip.

### Snapshot per request

`tool.ts:225-287`. `snapshot(permissions)` filters, splits into `direct` (`codemode === false`) vs `codeModeTools`, optionally synthesizes the CodeMode `execute` tool, and returns `{ definitions, codeModeCatalog?, execute }` (`tool.ts:51-63`). Called once per `select`: `context.ts:134`. Definitions are sorted by name for stable prefix caching (`tool.ts:258-260`). Catalog caching keyed on `(registry revision, visible tool-name string)` at `tool.ts:246-254`.

Separately, a **filesystem** snapshot is taken per attempt: `step.ts:73`

```ts
73:    const startSnapshot = yield* snapshots.capture()
```

passed into the publisher (`step.ts:74-82`) and diffed at step end into `files` (`step.ts:227-235`, `snapshot.ts:117-132`).

### Permission filtering removes tools from the request

`tool.ts:230-233`:

```ts
230:          for (const [name, tool] of data.tools) {
231:            if (whollyDisabled(tool.options?.permission ?? name, rules)) continue
232:            active.set(name, tool)
233:          }
```

`tool.ts:292-295`:

```ts
292: const whollyDisabled = (action: string, rules: Permission.Ruleset) => {
293:   const rule = rules.findLast((rule) => Wildcard.match(action, rule.action))
294:   return rule?.resource === "*" && rule.effect === "deny"
295: }
```

CodeMode's `execute` tool is gated the same way (`tool.ts:238`). A later request-level re-check exists in `execute` (`tool.ts:272-275`) for tools removed by a session-context hook:

```ts
274:              if (!requested && input.definitions && (direct.has(event.tool) || codeModeTool?.name === event.tool))
275:                return yield* new Tool.Error({ message: `Tool is not available for this request: ${event.tool}` })
```

### Parsing a tool call from the stream

The provider adapter emits `tool-input-start` / `tool-input-delta` / `tool-input-end` / `tool-input-error` then `tool-call` (`/home/linsir365/projects/opencode/packages/ai/src/schema/events.ts:190-239`). Core's publisher handles them at `/home/linsir365/projects/opencode/packages/core/src/session/runner/publish-llm-event.ts:439-477`; the `tool-call` branch marks `called`, closes the input fragment, and publishes `Tool.Called`:

```ts
459:      case "tool-call": {
460:        outputStarted = true
461:        const tool = tools.get(event.id) ?? (yield* startToolInput(event))
462:        if (toolInput.has(event.id)) yield* endToolInput(event)
...
466:        tool.called = true
467:        tool.providerExecuted = event.providerExecuted === true
468:        yield* bus.publish(SessionEvent.Tool.Called, {
```

Malformed JSON becomes a `tool.input-json` failure instead of an execution: `publish-llm-event.ts:316-339`, `:455-458`.

### Dispatch: one fiber per local tool call

`/home/linsir365/projects/opencode/packages/core/src/session/step.ts:104-133`:

```ts
117:          if (event.type !== "tool-call" || event.providerExecuted) return
118:          toolRuns.push({
119:            call: event,
120:            fiber: yield* Effect.uninterruptibleMask((restore) =>
121:              restore(executeTool(event)).pipe(
122:                Effect.flatMap(toolOutput.truncate),
123:                Effect.flatMap((outcome) => publisher.toolExecution(event.id, event.name, outcome)),
124:                Effect.catchTag("Tool.Error", (error) =>
125:                  publisher.failTool(event.id, toSessionError(error), error.metadata).pipe(Effect.asVoid),
126:                ),
127:              ),
128:            ).pipe(Effect.forkScoped),
129:          })
```

Forked inside the stream consumer, i.e. inside the attempt's scope (`step.ts:276` `Effect.scoped`). Provider-hosted tools (`providerExecuted`) are skipped entirely. Ordering invariant is stated at `publish-llm-event.ts:65-77`.

### Result attached to the assistant message

`step.ts:123` → `publisher.toolExecution` → `/home/linsir365/projects/opencode/packages/core/src/session/runner/publish-llm-event.ts:575-592`:

```ts
581:    tool.settled = true
...
584:    yield* bus.publish(SessionEvent.Tool.Success, {
585:      sessionID: input.sessionID,
586:      assistantMessageID,
587:      id,
588:      content,
```

Projection (`/home/linsir365/projects/opencode/packages/core/src/session/projector.ts:704`) hands it to the message reducer (`/home/linsir365/projects/opencode/packages/core/src/session/message-updater.ts:342-359`), which rewrites the tool content item's state:

```ts
349:            match.state = castDraft(
350:              SessionMessage.ToolStateCompleted.make({
351:                status: "completed",
352:                input: match.state.input,
353:                content: event.data.content,
```

On the next request, `to-llm-message.ts:213-229` lifts *local* results into their own `Message.tool(...)` messages so they follow the assistant message:

```ts
213:  const results = message.content
214:    .filter((item): item is SessionMessage.AssistantTool => item.type === "tool" && item.executed !== true)
```

### Per-tool output cap

`/home/linsir365/projects/opencode/packages/core/src/tool-output.ts:13-14`:

```ts
13: export const MAX_LINES = 2_000
14: export const MAX_BYTES = 50 * 1024 // 50 KiB
```

Configurable via `Editor.configure` (`tool-output.ts:27-29`, `:59-64`); applied at `tool-output.ts:67-118`, which spills the full text to `~/.local/share/opencode/tool-output/tool_<id>` and appends `[showing lines 1-N of M; full output saved to <file>]` (`:90`). It is skipped when a tool already set `metadata.truncated` (`tool-output.ts:68`). Retry-wired: `step.ts:122`.

---

## 6. THE PERMISSION LAYER

### Action + resource model

`/home/linsir365/projects/opencode/packages/schema/src/permission.ts:59-66`:

```ts
59: export const Rule = Schema.Struct({
60:   action: Schema.String,
61:   resource: Schema.String,
62:   effect: Schema.Literals("allow", "deny", "ask") ...
63: })
65: export const Ruleset = Schema.Array(Rule)
```

`AssertInput` = `{ sessionID, action, resources[], save[]?, metadata?, source?, agent? }` (`/home/linsir365/projects/opencode/packages/core/src/permission.ts:42-47`). Tools call `permission.assert({ action, resources, save, sessionID, agent, source })` from inside `execute`, e.g. `tool/plugin/shell.ts:135-142`, `tool/plugin/edit.ts:180-183`, `tool/plugin/subagent.ts:138-151`.

### Last-declared-wins + default-ask

`/home/linsir365/projects/opencode/packages/core/src/permission.ts:87-97`:

```ts
87: export function evaluate(action: string, resource: string, ...rulesets: Permission.Ruleset[]): Permission.Rule {
88:   return (
89:     rulesets
90:       .flat()
91:       .findLast((rule) => Wildcard.match(action, rule.action) && Wildcard.match(resource, rule.resource)) ?? {
92:       action,
93:       resource: "*",
94:       effect: "ask",
95:     }
96:   )
97: }
```

The `?? { effect: "ask" }` branch *is* the default-ask. `merge` is plain concatenation, so ordering across sources is the array order (`permission.ts:99-101`); the agent's ruleset precedes the session's (`permission.ts:162`), and the session's ruleset is what narrowed `Tool.snapshot` in the first place (`context.ts:131`).

### Glob matching`/home/linsir365/projects/opencode/packages/core/src/util/wildcard.ts:3-14`:

```ts
 3: export function match(input: string, pattern: string) {
 4:   const normalized = input.replaceAll("\\", "/")
 5:   let escaped = pattern
 6:     .replaceAll("\\", "/")
 7:     .replace(/[.+^${}()|[\]\\]/g, "\\$&")
 8:     .replace(/\*/g, ".*")
 9:     .replace(/\?/g, ".")
10:
11:   if (escaped.endsWith(" .*")) escaped = escaped.slice(0, -3) + "( .*)?"
12:
13:   return new RegExp("^" + escaped + "$", process.platform === "win32" ? "si" : "s").test(normalized)
14: }
```

### Multi-resource evaluation

`permission.ts:165-189`:

```ts
165:    function denied(input: Pick<Request, "action" | "resources">, rules: Permission.Ruleset) {
166:      return input.resources.some((resource) => evaluate(input.action, resource, rules).effect === "deny")
167:    }
...
176:      const all = [...rules, ...(yield* savedRules())]
177:      const effects = input.resources.map((resource) => evaluate(input.action, resource, all).effect)
178:      const effect: Permission.Effect = effects.includes("ask") ? "ask" : "allow"
179:      const event = yield* hooks.trigger("permission", "evaluate", { ... })
188:      return { effect: event.effect, message: event.message, rules: all }
```

Config-only deny is authoritative (`deny` wins before saved allow-rules are consulted); among config + saved, `ask` beats `allow`; the `permission.evaluate` plugin hook can override the result.

`assert` (`:231-263`) then: `deny` → `BlockedError`; `allow` → return; `ask` → create a pending request and `Deferred.await`. The decline deliberately tunnels through the **defect** channel (`:249-253`):

```ts
249:              // Deliberate defect tunnel: leaves wrap execution in blanket `mapError`, which
250:              // must not convert a user's decline into model-facing tool output. The decline
251:              // resurfaces as a typed failure at SessionModelRequest.executeTool.
253:              Effect.catchTag("Permission.DeclinedError", (error) => Effect.die(error)),
```

and is recovered as a typed failure in `/home/linsir365/projects/opencode/packages/core/src/session/model-request.ts:385-399`.

### "always allow" persistence

`permission.ts:299-305`:

```ts
299:          if (input.reply === "always" && existing.request.save?.length) {
300:            yield* saved.add({
301:              projectID: location.project.id,
302:              action: existing.request.action,
303:              resources: existing.request.save,
304:            })
305:          }
```

Rows land in `permission` (`/home/linsir365/projects/opencode/packages/core/src/permission/saved.ts:63-78`, `.onConflictDoNothing()`), scoped to the project, and are re-read on every evaluation as `{ effect: "allow" }` rules (`permission.ts:148-156`). `always` also sweeps sibling pending asks that would now evaluate to `allow` (`permission.ts:308-322`). `reject` fails the *whole* batch for the session (`permission.ts:282-296`).

### How a denied tool disappears from the model's tool list

Only a `deny` rule with `resource === "*"` removes the definition (`tool.ts:231`, `tool.ts:292-295`). A resource-scoped deny (`resources: [target.resource]` in `tool/plugin/edit.ts:182`) leaves the tool advertised; it fails at `assert` → `BlockedError` → `ToolFailure` → `Tool.Failed` event → `ToolStateError` on the assistant message (`message-updater.ts:360-378`). Also `Agent.mode === "primary"` / `!agent.hidden` / per-agent deny filter the *subagent* list out of the tool description (`tool/plugin/subagent.ts:288-295`).

---

## 7. THE EVENT SYSTEM

`/home/linsir365/projects/opencode/packages/core/src/bus.ts`.

### Publish / subscribe

```ts
139: export interface Interface {
140:   readonly publish: <D extends Event.Definition>(definition: D, data: Event.Data<D>, options?: PublishOptions)
144:     => Effect.Effect<Event.Payload<D>>
145:   readonly publishAll: <const I extends readonly [PublishInput, ...PublishInput[]]>(events: I) => ...
148:   readonly subscribe: Subscribe
156:   readonly log: (input: { aggregateID; after?; follow? }) => Stream.Stream<LogItem>
163:   readonly project: <D>(definition: D, projector: Subscriber<D>) => Effect.Effect<void>
164:   readonly replay: (event: SerializedEvent, options?) => Effect.Effect<void>
168:   readonly remove: (aggregateID: string) => Effect.Effect<void>
169:   readonly claim: (aggregateID: string, ownerID: string) => Effect.Effect<void>
```

Three pubsub tiers (`bus.ts:193-197`): `live` (all), `typed` (per type), `durable` (per-aggregate wake psliding(1)). `subscribe()` with no argument is a volatile live stream; `subscribe(def)` uses the typed channel (`bus.ts:748-762`), filtered by ambient Location (`local`, `bus.ts:726-746`).

### Durable events are written in the same transaction as their projections

`bus.ts:314-434`. Sequence allocation, projection, local `commit` hook, and the event row are all inside one `db.transaction(..., { behavior: "immediate" })`:

```ts
321:                        const row = yield* db
322:                          .select({ seq: EventSequenceTable.seq, ownerID: EventSequenceTable.owner_id })
...
327:                        const latest = row?.seq ?? -1
...
371:                        const seq = input?.seq ?? latest + 1
...
399:                        const route = yield* prepareRoutes([committed])
400:                        for (const projector of list) {
401:                          yield* projector(committed)
402:                        }
403:                        if (commit) yield* commit(seq)
404:                        yield* db
405:                          .insert(EventSequenceTable)
...
416:                        if (persist)
417:                          yield* db
418:                            .insert(EventTable)
```

Yes — **projections run in the same transaction**. Serialization is per-aggregate via `durableLocks.withLock(aggregateID)` (`bus.ts:469-477`), also in `publishAll` (`bus.ts:575`) and `replay` (`bus.ts:681`). Notification happens only after commit (`bus.ts:436-444`); routes are applied only after the transaction commits (`bus.ts:436-437`, and the comment at `bus.ts:252-253`).

Local non-durable projections via `PublishOptions.commit` are documented as excluded from replay (`:96-97`) and require a durable event (`:456-462`). The execution claim/release rides this hook: `/home/linsir365/projects/opencode/packages/core/src/session/execution.ts:81-86`.

### `persist` default

`bus.ts:174-203`:

```ts
176:   /** Maximum durable rows read per page while replaying or tailing an aggregate log. */
177:   readonly logReadPageSize?: number
178:   /** Retain durable event payloads for historical log reads and replay. */
179:   readonly persist?: boolean
...
202:        const logReadPageSize = options?.logReadPageSize ?? 512
203:        const persist = options?.persist ?? false
```

**`false`.** Default `Bus.node = configured()` (`bus.ts:908`); the server overrides it from options (`/home/linsir365/projects/opencode/packages/server/src/routes.ts:118`). Tables: `event_sequence` and `event` (`/home/linsir365/projects/opencode/packages/core/src/event/sql.ts:4-26`).

### Replay

`bus.ts:671-701`. It resolves the definition from the durable manifest, locks the aggregate, and re-enters `commitDurableEvent` with the recorded `seq`. Divergence is fatal (`bus.ts:360-366`):

```ts
361:                          yield* Effect.die(
362:                            new InvalidDurableEventError({
363:                              type: event.type,
364:                              message: `Replay diverged at aggregate ${aggregateID} sequence ${input.seq}`,
```

Idempotent re-apply is allowed when the stored row is byte-identical (`isDeepStrictEqual`, `bus.ts:346-359`). Owner fencing: `strictOwner` mismatch dies (`bus.ts:329-336`), a plain owner mismatch returns without writing (`bus.ts:368-370`). `notify` after replay only when `options.publish` (`bus.ts:695-697`).

The `log` stream (`bus.ts:828-871`) subscribes **before** the historical read so events committed mid-replay land in the read or arrive via a post-marker wake (`bus.ts:853-855`), then emits one `log.synced` marker (`bus.ts:857-862`).

---

## 8. SUB-AGENTS

`/home/linsir365/projects/opencode/packages/core/src/tool/plugin/subagent.ts`.

### Child session creation

`:184-198`:

```ts
184:              const model = override ?? agent.model ?? parent.model
185:              const child =
186:                existing ??
187:                (yield* sessions
188:                  .create({
189:                    parentID: context.sessionID,
190:                    title: input.description,
191:                    agent: Agent.ID.make(input.agent),
192:                    model,
193:                  })
```

Then the child is prompted with a framing preamble, `:205-213`:

```ts
205:              yield* sessions
206:                .prompt({
207:                  sessionID: child.id,
208:                  text:
209:                    existing === undefined
210:                      ? ["You are a subagent spawned by another session.", input.prompt].join("\n")
211:                      : input.prompt,
212:                  ...(background && existing === undefined ? { resume: false } : {}),
213:                })
```

Reuse is allowed only within the same parent — `:164-167`:

```ts
164:              if (existing !== undefined && existing.parentID !== context.sessionID)
165:                return yield* new ToolFailure({
166:                  message: `Session ${existing.id} is not a child of the current session`,
```

### Depth limit

`:117-133`:

```ts
118:              let depth = 0
119:              while (current.parentID) {
120:                depth++
...
129:              const limit = Config.latest(yield* config.entries(), "experimental")?.subagent_depth ?? 1
130:              if (depth >= limit)
131:                return yield* new ToolFailure({
132:                  message: `Subagent depth limit reached (${limit}). Increase "experimental.subagent_depth" to allow nested subagents.`,
```

**Default depth 1** — no nesting unless `experimental.subagent_depth` is raised. Config plumbing at `/home/linsir365/projects/opencode/packages/core/src/config/normalize.ts:424-431`.

### What tools the child gets

Nothing is inherited by copying: the child is a full session with its own `select`, so its tool snapshot is derived from its own agent's permissions (`context.ts:128-134`). The only narrowing is (a) `agent.mode === "primary"` is refused (`:136-137`), and (b) a permission assert before spawning (`:138-151`). The `subagent` tool itself is registered with `options: { codemode: false }` (`:104`), and its own description is rewritten per-request by a `session.context` hook (`:282-307`).

### Do the child's events reach the parent?

**Not as events.** The child has its own event aggregate. Completion is funneled back as a synthetic message in the *parent's* inbox — `/home/linsir365/projects/opencode/packages/core/src/session/subagent-completion.ts:36-43`:

```ts
36:  yield* sessions.synthetic({
...
38:    sessionID: recovery.parentSessionID,
...
41:    text: `<subagent sessionID="${recovery.childSessionID}" state="${input.status}" description="${recovery.description}">\n${text}\n</subagent>`,
42:    metadata: { source: "subagent", childID: recovery.childSessionID, agent: recovery.agent, state: input.status },
```

The parent therefore sees a user-role message (default `delivery: "steer"`, `session.ts:296`) and can be steered into reacting to it.

### Can a child spawn children?

Yes, structurally — but only if depth is raised. A child *is* a session and *does* get the `subagent` tool if its agent's permissions allow; the `depth >= limit` check is the only gate.

### Blocking vs background

Foreground blocks on `jobs.block` with interrupt propagation (`:234-240`); `background: true` returns immediately (`:19-27`, `:229-232`). Job creation and completion observers: `/home/linsir365/projects/opencode/packages/core/src/session/subagent-job.ts:36-59`.

---

## 9. MEMORY / INSTRUCTIONS

There is **no memory layer**. What exists is a content-addressed instruction system with epochs.

### Instruction sources and hashing

`/home/linsir365/projects/opencode/packages/core/src/instructions/index.ts:33-39`:

```ts
33: export interface Source {
34:   readonly key: Key
35:   readonly read: Effect.Effect<Schema.Json | Unavailable | Removed>
36:   readonly initial: (value: Schema.Json) => string | undefined
37:   readonly changed: (previous: Schema.Json, current: Schema.Json) => string | undefined
38:   readonly removed: (previous: Schema.Json) => string | undefined
39: }
```

Values are sha256 over canonical JSON (`instructions/index.ts:194-196`, `243-251`); `diff` produces a `{key: hash|removed}` delta plus the blobs (`instructions/index.ts:142-159`). Unavailable sources block *initialization* only (`:143-144`):

```ts
143:  const blocked = previous ? [] : observed.flatMap((entry) => (isUnavailable(entry.value) ? [entry.key] : []))
144:  if (blocked.length > 0) return Effect.fail(new InitializationBlocked({ keys: blocked }))
```

### Epochs

`/home/linsir365/projects/opencode/packages/core/src/session/instruction-state.ts:38-61` publishes `SessionEvent.InstructionsUpdated` carrying the delta plus the **frozen rendered text**, with a `commit` hook writing the blobs:

```ts
45:    // The rendered text is frozen into the durable event because re-rendering it
46:    // later would require the original Location-scoped instruction sources.
```

`InstructionStateTable` has `epoch_start`, `through_seq`, `initial_values`, `current_values` (`/home/linsir365/projects/opencode/packages/core/src/session/sql.ts:167-176`). The epoch is advanced on compaction completion — `advanceEpoch` (`instruction-state.ts:139-154`), called from `/home/linsir365/projects/opencode/packages/core/src/session/projector.ts:710-715`:

```ts
713:          yield* InstructionState.advanceEpoch(db, event.data.sessionID, event.durable.seq)
```

i.e. the epoch baseline is re-cut from the then-current values, and each subsequent native checkpoint supersedes the chronological updates before it (`history.ts:99-107`).

### AGENTS.md

`/home/linsir365/projects/opencode/packages/core/src/config/plugin/instruction.ts:37-50`:

```ts
37:      const globalFile = yield* fs.resolve(join(global.config, "AGENTS.md"))
...
43:      const candidates = [
44:        ...(discovery.global ? [globalFile] : []),
45:        ...(project
46:          ? ancestorDirectories(start, stop)
47:              .map((directory) => join(directory, "AGENTS.md"))
```

Walked upward with `fs.up({ targets: ["AGENTS.md"], start, stop, type: "file" })` (`:71`), debounced 100 ms on watcher events (`:112-116`), rendered under key `core/instructions` (`/home/linsir365/projects/opencode/packages/core/src/instruction-discovery.ts:17`) and changed-rendered as a unified diff (`instruction-discovery.ts:127-148`). The `read` tool additionally discovers AGENTS.md around files it reads and injects them as durable synthetic instructions (`/home/linsir365/projects/opencode/packages/core/src/tool/plugin/read.ts:16`, `:82-99` → `/home/linsir365/projects/opencode/packages/core/src/session/instructions.ts:74-79`).

### The other "state" surfaces

- `InstructionEntry` — session-scoped API context, one source per key, `MaxValueBytes` enforced (`/home/linsir365/projects/opencode/packages/core/src/session/instruction-entry.ts:12-18`, `:134-145`).
- Skills — `/home/linsir365/projects/opencode/packages/core/src/skill/instructions.ts:82`.
- Compaction checkpoint — the only cross-turn summary (`compaction.ts:248-252`).

---

## 10. PROVIDER / ERROR RECOVERY

### Classification

`/home/linsir365/projects/opencode/packages/ai/src/provider-error.ts:235-305` — `classifyProviderFailure` returns one of 12 reasons (`/home/linsir365/projects/opencode/packages/ai/src/schema/errors.ts:142-155`). Context overflow is a *classification* on `InvalidRequestError` (`:249-256`), matched by ~30 message patterns with exclusions (`:16-68`).

### Retryability

`provider-error.ts:81-116`:

```ts
82:   const override = error.reason.http?.headers["x-should-retry"]
83:   if (override === "true") return true
84:   if (override === "false") return false
85:   switch (error.reason._tag) {
86:     case "RateLimit":
87:     case "ProviderInternal":
88:       return true
...
91:     case "Transport":
92:       return (
93:         error.reason.delivery !== "rejected" &&
94:         (error.reason.delivery !== "accepted" || error.reason.operation === "read")
95:       )
...
101:     case "UnknownProvider":
102:       return true
```

### Retry policy

`/home/linsir365/projects/opencode/packages/core/src/session/runner/retry.ts:42-92`:

```ts
42: // Exponential from 2s capped at 10s per gap, for 10 retries: 2, 4, 8, then 10 × 7, about 84s of
43: // waiting when every attempt fails (67–101s with jitter). ...
45: const schedule = Schedule.max([
46:   Schedule.min([Schedule.exponential("2 seconds"), Schedule.spaced("10 seconds")]),
47:   Schedule.recurs(10),
48: ]).pipe(
49:   Schedule.jittered,
...
51:   Schedule.modifyDelay(({ input, duration: delay }) => {
52:     const minimum = retryAfter(input)
53:     const duration = minimum === undefined ? delay : Duration.max(delay, Duration.millis(minimum))
```

`RETRY_AFTER_MAX = 15 minutes` (`:32`), `MAX_TIMEOUT_RETRIES = 3` (`:60`), and a plugin hook that can veto or rewrite the decision (`:78-90`). Waiting publishes `RetryScheduled` and sleeps (`retry.ts:97-113`).

### compaction vs retry vs continue

All decided in `step.ts:147-273`:

| Outcome | Condition | file:line |
|---|---|---|
| **Compacted** | overflow failure, `!record.outputStarted`, and `recoverOverflow` succeeded | `step.ts:148-159` |
| **RecoverFull** | `Transport` failure with `recovery ∈ {retry-full, rotate-and-retry-full}`, `!outputStarted` | `step.ts:174-180` |
| **Retry** | retryable, `retry.retry`, `!outputStarted` | `step.ts:192-196` |
| **Continue** | retryable, `outputStarted`, no declines, no interrupted tools | `step.ts:253-261` |
| **Completed** | terminal | `step.ts:271-273` |

```ts
148:        if (
149:          !publisher.record().outputStarted &&
150:          isContextOverflowFailure(overflow) &&
151:          (yield* restore(input.recoverOverflow))
152:        ) {
153:          yield* Effect.logWarning("provider rejected the request as too long; compacting", {
...
158:          return Outcome.Compacted()
```

`recoverOverflow` is one-shot (`llm.ts:262-268` sets nothing; `llm.ts:288-290` `recoverOverflow = false`), and `recoverContinuation` likewise (`llm.ts:212`, `:291-293`). The `Continue` path injects a synthetic nudge and starts a fresh assistant message (`llm.ts:278-286`, `CONTINUE_AFTER_INCOMPLETE_STREAM` at `llm.ts:35-36`).

### Fallback chain

Two independent fallbacks, both transport-level:

1. **WebSocket → HTTP**, after 5 consecutive stream failures (`/home/linsir365/projects/opencode/packages/core/src/session/model-transport.ts:27`, `:186-197`):
   ```ts
   190:        if (owner.streamFailures < MAX_STREAM_FAILURES) return
   191:        owner.httpFallback = true
   ```
   `owner.httpFallback` short-circuits the WS bind (`model-transport.ts:308`).
2. **Oversized frame → exchange fallback** (`model-transport.ts:495-496`).

There is **no model-level fallback chain** in the orchestration layer — `SessionRunnerModel.resolve` either returns the selected model or fails with `ModelUnavailableError` (`/home/linsir365/projects/opencode/packages/core/src/session/runner/model.ts:75-91`). `TransportError.recovery` is a 5-value enum (`errors.ts:118-120`) but only `retry-full` and `rotate-and-retry-full` are consumed, both collapsing into `Outcome.RecoverFull` (`step.ts:176-177`) — i.e. "rebuild the request from scratch against the same model".

Stale-work settlement on drain entry (`llm.ts:299-352`): orphaned `running` compactions become `CompactionFailed`; orphaned `streaming`/`running` tool calls become `Tool.Failed`.

---

## 11. THE 5 STEP RESULTS

`/home/linsir365/projects/opencode/packages/core/src/session/runner/step.ts:32-42`:

```ts
32: export type Outcome = Data.TaggedEnum<{
33:   Completed: { readonly needsContinuation: boolean }
34:   Retry: { readonly error: SessionError.Error; readonly decision: SessionRunnerRetry.Decision }
35:   Continue: {
36:     readonly error: SessionError.Error
37:     readonly decision: SessionRunnerRetry.Decision
38:   }
39:   RecoverFull: {}
40:   Compacted: {}
41: }>
42: export const Outcome = Data.taggedEnum<Outcome>()
```

Production sites and handling:

| Variant | Produced at | Handled at |
|---|---|---|
| `Completed` | `step.ts:265` (location closed, after decline) and `step.ts:271-273` (normal terminal, `needsContinuation = toolChoice !== "none" && record.needsContinuation`) | `llm.ts:271` |
| `Retry` | `step.ts:192-196` — `llmFailure && llmError && retry?.retry && !recorded.outputStarted`; calls `publisher.startAssistant()` first so retry state projects onto a real assistant | `llm.ts:272-277` → `retry.wait` |
| `Continue` | `step.ts:253-261` — same, but `record.outputStarted` and no declines/interrupts | `llm.ts:278-286` → `retry.wait` + publish `SessionEvent.Synthetic` + new assistant id |
| `RecoverFull` | `step.ts:174-180` — Transport `retry-full`/`rotate-and-retry-full` with `!outputStarted` | `llm.ts:291-293` (`recoverContinuation = false`) |
| `Compacted` | `step.ts:158` — overflow with recovery available and `!outputStarted` | `llm.ts:287-290` (`recoverOverflow = false`, new assistant id) |

`needsContinuation` originates from `StepRecord` (`/home/linsir365/projects/opencode/packages/core/src/session/runner/publish-llm-event.ts:615-618`):

```ts
615:      needsContinuation: Iterable.some(
616:        tools.values(),
617:        (tool) => !tool.providerExecuted && (tool.called || tool.settled),
618:      ),
```

Non-`Outcome` exits from `attempt`: `Effect.failCause(stream.cause)` (`:263`), `Effect.interrupt` on a tool decline while the Location is open (`:264-267`), `Effect.failCause` for interrupted tools (`:268-269`), and `StepFailedError` (`:270`).

---

## 12. THE MAIN DATA FLOW

Ordered concrete calls, "user sends prompt" → "persisted message".

**Admission (synchronous, HTTP request thread)**

1. `packages/server/src/handlers/session.ts:308-342` — `session.prompt({sessionID, id?, text, files?, agents?, skills?, metadata?, delivery?, resume?})`
2. `packages/core/src/session/session.ts:145` — `Effect.fn("Session.prompt")`, wrapped in `Effect.uninterruptibleMask`
3. `session.ts:148` — `get(sessionID)` → `SessionStore.get` (`store.ts:95`)
4. `session.ts:149` — `messageID = input.id ?? SessionMessage.ID.create()`
5. `session.ts:151` — `admission.reconcile({id, sessionID, type:"user", delivery: input.delivery ?? "steer"})` → `inbox.ts:155`
6. `session.ts:159` — `SessionPrompt.prepare({session, messageID, input})` → `prompt.ts:29` (plugin hook `session.prompt` at `prompt.ts:40`, attachment materialization `prompt.ts:54`, skill resolution `prompt.ts:57-76`)
7. `session.ts:165` — `SessionRevert.commit(bus, session)` if a revert was staged
8. `session.ts:166` — `admission.admit({id, sessionID, item})` → `inbox.ts:169`
9. `inbox.ts:177` — `bus.publish(SessionEvent.InboxEnqueued, {inboxID, sessionID, item})` → `bus.ts:507` → `bus.ts:450` → `bus.ts:284 commitDurableEvent`
10. `bus.ts:317` — `db.transaction(...)`: seq allocated, then projectors run **inside** the tx
11. `projector.ts:641` → `inbox.ts:277 projectAdmitted` → `INSERT INTO session_inbox ... ON CONFLICT DO NOTHING` (`inbox.ts:294-315`); `projector.ts:650-655` bumps `time_updated`
12. `bus.ts:436-444` — post-commit route apply + durable wake notifications
13. `session.ts:174` — `execution.wake(sessionID)` → `execution.ts:172` → `run-coordinator.ts:136` (**fire-and-forget**)
14. `session.ts:175` — returns `admitted` to HTTP

**Execution (separate fiber, started by the wake)**

15. `run-coordinator.ts:144` → `start(key, false, scope)` → `run-coordinator.ts:87`
16. `run-coordinator.ts:97-110` — `fork(Effect.yieldNow → started() → loop(key, execution, force))`
17. `run-coordinator.ts:99` → `options.started` → `execution.ts:112-116` — `bus.publish(SessionEvent.Execution.Started, ..., {commit: store.claim})`
18. `run-coordinator.ts:75` → `options.drain` → `execution.ts:87 drain`
19. `execution.ts:95-97` — `SessionRunner.Service.use(runner => runner.drain({sessionID, force, continuation, promotable})).pipe(instances.provide(session))`
20. `llm.ts:54` — `SessionRunner.drain`
21. `llm.ts:62` — `SessionInbox.nextPromotable(db, sessionID, "input")` (early-exit guard, `:63`)
22. `llm.ts:67` — `plugins.awaitActivation`
23. `llm.ts:68` — `settleStaleCompactions(sessionID)` (`llm.ts:299`)
24. `llm.ts:69` — `settleStaleToolCalls(sessionID)` (`llm.ts:329`)
25. `llm.ts:189` → `llm.ts:71 advanceToStep` → `llm.ts:74 while (true)`
26. `llm.ts:75` — `lifecycle.isClosed()` guard → `DrainResult.Reloaded` (`:77`)
27. `llm.ts:80` — `SessionInbox.serialized(sessionID, ...)` (per-session `KeyedMutex`, `inbox.ts:62-63`)
28. `llm.ts:83-87` — `SessionInbox.nextPromotable(db, sessionID, entering || !continuing ? "input" : "steer")`
29. compaction inbox item → `llm.ts:89-92` `publishAll([InboxDelivered, Compaction.Started])`; move item → `llm.ts:94-102` (`modelTransport.close` + `publishAll([InboxDelivered, Moved])`)
30. `llm.ts:165` → `llm.ts:198 prepareContext` → `context.ts:121 SessionContext.select`
    - `context.ts:127` `mcpTools.flush`; `:128` `agents.select`
    - `context.ts:131` `Permission.merge(agent.permissions, session.permissions)`
    - `context.ts:134` `registry.snapshot(permissions)` → `tool.ts:225`
    - `context.ts:135-140` instructions/skills/references/mcp/entries loaded, `concurrency: "unbounded"`
    - `context.ts:150-158` `Instructions.combine([...])`
31. `llm.ts:201` — `InstructionState.prepare(db, bus, selected.instructions, sessionID)` → `instruction-state.ts:80` → `observe` (`:21`) → `commit` (`:38`) → `bus.publish(SessionEvent.InstructionsUpdated, ..., {commit: insertBlobs})`
32. `llm.ts:166` — `SessionInbox.promote(db, bus, sessionID, entering && !continuing ? promotable : "steer")` → `inbox.ts:496`
33. `inbox.ts:470` — `bus.publish(SessionEvent.InboxDelivered, {sessionID, inboxID})`
34. `projector.ts:609` → `inbox.ts:324 projectDelivered` (DELETE … RETURNING) then `projector.ts:616 insertMessage` → `projector.ts:386` — **the user message is now persisted** with `seq = event.durable.seq` (`projector.ts:395`)
35. `llm.ts:174-177` — title generation forked via `FiberMap.run(titles, ...)` (non-blocking)
36. `llm.ts:179` — `context.load(selected)` → `context.ts:163`; `context.ts:164` `resolveModel`; `context.ts:165` `SessionHistory.entriesForRunner` → `history.ts:118` (tx: `messageEntries` + `InstructionState.initial`)
37. `llm.ts:190` — `_tag === "Ready"` → `llm.ts:191 runStep(next.context, step)` → `llm.ts:206`
38. `llm.ts:217` — `compaction.compact({reason:"auto", context: loaded})` → `compaction.ts:199` (may `continue` with a new assistant id, `llm.ts:219-222`)
39. `llm.ts:224` — `SessionModelRequest.baseTranscript({agent, model, tools, initial, messages})` → `model-request.ts:100` (2 system parts + `toLLMMessages`)
40. `llm.ts:231` — `context.request.primary({...})` → `model-request.ts:408` → `model-request.ts:219 prepare` → hooks `session.context` (`:403-405`) → `LLM.request` (`:262`) → `model-request.ts:385 executeTool` closure built
41. `llm.ts:245` — `steps.attempt({...})` → `step.ts:72`
42. `step.ts:73` — `snapshots.capture()` → `snapshot.ts:117`
43. `step.ts:74` — `createLLMEventPublisher(bus, {...})` → `publish-llm-event.ts:78`
44. `step.ts:104` — `llm.stream(input.prepared.request, input.prepared.options).pipe(Stream.runForEach(...))`
45. `step.ts:116` — `publisher.publish(event)` → `publish-llm-event.ts:401` → e.g. `Step.Started` (`:103`), `Text.Delta` (`:221`), `Tool.Called` (`:468`)
46. `step.ts:118-128` — on `tool-call`: `Effect.forkScoped(executeTool(event) → toolOutput.truncate → publisher.toolExecution)`
47. `model-request.ts:386` — `tools.execute({...call, definitions: hooked})` → `tool.ts:263` → `tool.ts:119 execute` (schema decode `tool/runtime.ts:30`) → tool body → `permission.assert` (`shell.ts:135` → `permission.ts:231`) → `Permission.Event.Asked` (`permission.ts:216`)
48. `step.ts:122` — `toolOutput.truncate` → `tool-output.ts:67` (2000 lines / 50 KiB, spill file)
49. `step.ts:123` → `publish-llm-event.ts:584` — `bus.publish(SessionEvent.Tool.Success, {...})`
50. `projector.ts:704` → `message-updater.ts:342` — assistant message content item → `ToolStateCompleted` (`message-updater.ts:349-356`)
51. `step.ts:241-250` — `bus.publish(SessionEvent.Step.Ended, {..., snapshot, files})`
52. `projector.ts:686` — `run(db, event)` + `applyUsage` → `time_completed`, `finish`, `cost`, `tokens`, snapshot (`message-updater.ts:262-272`)
53. `llm.ts:270` — `SessionStep.Outcome.$match(outcome, {...})`
54. `llm.ts:295` — `undefined` ⇒ retry the attempt in `runStep`'s `while (true)` (`llm.ts:213`); boolean ⇒ `llm.ts:191` returns to the drain loop
55. `llm.ts:162` — nothing promotable ⇒ `DrainResult.Complete()` ⇒ `run-coordinator.ts:75` returns
56. `run-coordinator.ts:78-82` — doorbell rung? overwrite `scope`, trampoline into another drain; else end the loop
57. `run-coordinator.ts:101-105` — `settled(key, exit, reason)` → `execution.ts:119-147` — `Execution.Succeeded` / `.Interrupted` / `.Failed` with `{commit: store.release}`
58. `run-coordinator.ts:116-120` — `settle`: successor execution if `pendingWake`, else `executions.delete(key)`; `Deferred.doneUnsafe`
59. `projector.ts:671-673` — `projectIdle` → `time_idle` monotone watermark + `idle_outcome` (`projector.ts:420-430`)
60. `run-coordinator.ts:131` — joiners of `run` (`SessionExecution.resume`) resolve with this exit; `llm.ts:178-190` uses `awaitIdle` to follow successors

Independent of the request thread, subagents spawn at `subagent.ts:187-213` and report back at `subagent-completion.ts:36`.