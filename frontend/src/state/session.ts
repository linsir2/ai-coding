/**
 * Session state for the transcript (M2) — a pure reducer over wire events.
 *
 * The reducer is the single place that turns wire data into what the UI
 * shows; every string here is pinned by test/reducer.test.ts and ports the
 * retired Textual transcript's wording (ui/blocks.py) plus the four approved
 * M2 deltas.  Components stay thin: they render parts, they never derive.
 *
 * Updates are immutable but referentially stable: untouched messages and
 * parts keep their identity so Solid's keyed rendering only touches what
 * changed (one growing text part must not remount the whole transcript).
 */

import type { EventMessage } from "../ipc"
import type { PromptImage } from "../util/attachment"
import { foldToolChunk, shorten, toolTarget, toolVerb } from "../util/toolmeta"
import type { TailLine, ToolStream } from "../util/toolmeta"

export type Tone = "muted" | "error"

export type Part =
  | { kind: "text"; text: string; streaming: boolean }
  | { kind: "reasoning"; text: string }
  | {
      kind: "tool"
      callId: string
      toolName: string
      verb: string
      target: string
      status: "running" | "ok" | "failed"
      /** The live output, line by line, each tagged with the pipe it came off. */
      lines: TailLine[]
      /** Text not yet terminated by a newline; joined with the next chunk. */
      pending: string
      /** True once the budget forced the oldest lines out. */
      linesDropped: boolean
      preview: string
      error: string
      /** What the wire measured; 0 when the event carried none. */
      durationMs: number
    }
  | { kind: "plan"; goal: string; items: PlanItem[] }
  | { kind: "notice"; tone: Tone; text: string; action?: "resume" }
  | { kind: "image"; name: string }
  | { kind: "turn-end"; tone: "error"; text: string }
  | {
      kind: "compaction"
      status: "running" | "done" | "failed" | "skipped"
      reason: string
      text: string
      beforeMessages: number
      afterMessages: number
      inputTokens: number
      outputTokens: number
      totalTokens: number
      error: string
    }

export interface ChatMessage {
  role: "user" | "assistant"
  parts: Part[]
}

/** One todo entry on the plan card (20 §批次2) — live off ``plan.updated``.
 * D5: the card shows what approval will run, so the proof command and the
 * dependencies ride along -- the same two keys the review card reads. */
export interface PlanItem {
  id: string
  content: string
  status: string
  /** R1-1: the shell proof for this step ("" = not machine-checkable). */
  verify: string
  /** Item ids this step waits on (plan ids are "1".."n", as displayed). */
  dependsOn: string[]
}

/** One background shell job, as the panel renders it (D-1b). */
export interface JobRow {
  id: string
  command: string
  status: string
  pid: number
  logPath: string
  detail: string
  adopted: boolean
  /** M4b: the sidebar's job rows show when a run began (wire ``startedAt``). */
  startedAt: string
}

/** M4b: the session card's budget figures (same numbers ``/budget`` renders). */
export interface BudgetFigures {
  tokens: number
  cost: number | null
  cachedPercent: number | null
}

/** One queued line of operator input (D-2).  The id is the inbox row's
 * id on the backend, or a `local-` id when the socket was dead at submit time
 * and the row lives only here.  Dispatch deletes by id, best-effort: the
 * server ignores ids it never saw, so a local-only row dispatches exactly
 * like a persisted one and simply leaves nothing to delete.
 */
export interface QueuedLine {
  id: string
  text: string
}

export interface SessionState {
  messages: ChatMessage[]
  /** True from submit until the turn's finish event; gates the prompt. */
  busy: boolean
  /**
   * D7: the turn this client started and has not seen end (the ack's id).
   * A terminal event only settles if it names this turn -- a command's ending
   * can land while the prompt submitted after it is still running.
   */
  turnId?: number
  /** Lines typed while a turn was running; one is dispatched per finished turn. */
  queued: QueuedLine[]
  /**
   * The workspace's background jobs (D-1b).  Deliberately NOT a transcript
   * line: a running command is not something the user said, and folding it into
   * the scrollback would put words in their conversation.
   */
  jobs: JobRow[]
  /**
   * The goal episode's live status (G2), fed by plan.updated's orchestration
   * fields; the sidebar's goal line reads it.  Null until a task event says
   * otherwise -- a live indicator, not a snapshot (the sidebar line only
   * shows while armed).
   */
  goal: GoalStatus | null
}

/** The goal episode as the wire reports it (plan.updated, G2). */
export interface GoalStatus {
  goal: string
  armed: boolean
  roundsRun: number
  tokensUsed: number
  phase: string
}

export type Action =
  | { kind: "submitted"; text: string; images?: PromptImage[] }
  // 08: a resumed turn carries no prompt -- busy with no echo.  Reusing
  // "submitted" with an empty text would draw an empty user bubble.
  | { kind: "resumed" }
  // D: a command the route dispatched (the auto-run's "/plan run") is the
  // server working, not the operator speaking: busy, and no bubble.  The
  // command's own terminal "turn.finished" is what frees the composer
  // again -- the same contract a typed command has always had.
  | { kind: "command-started" }
  /** D7: the backend named the turn this request just started (the ack). */
  | { kind: "turn-started"; turnId: number }
  | { kind: "queued"; id: string; text: string }
  | { kind: "queued-dispatched" }
  /** Replace the list with the backend's rows (mount, reload, panel edits). */
  | { kind: "queued-loaded"; rows: QueuedLine[] }
  | { kind: "queued-edited"; id: string; text: string }
  | { kind: "queued-deleted"; id: string }
  | { kind: "submit-failed"; reason: string }
  // r20/F5: a paste the backend refused.  Not a failed submit -- the turn runs,
  // just without the picture -- so `busy` is left alone and the notice lands on
  // the user message that drew the image.
  | { kind: "attachment-rejected"; items: { name: string; reason: string }[] }
  | { kind: "wire"; event: EventMessage }
  /** The panel's rows, from job.changed or the session.jobs snapshot. */
  | { kind: "jobs"; jobs: JobRow[] }

export function initialState(): SessionState {
  return { messages: [], busy: false, queued: [], jobs: [], goal: null }
}

/** Narrow one wire row into the shape the panel draws. */
export function toJobRow(value: unknown): JobRow {
  const row = obj(value)
  return {
    id: str(row["id"]),
    command: str(row["command"]),
    status: str(row["status"]),
    pid: num(row["pid"]),
    logPath: str(row["log_path"]),
    detail: str(row["detail"]),
    adopted: row["adopted"] === true,
    startedAt: str(row["startedAt"]),
  }
}

/** The rows behind a job.changed event, or the ones already in state. */
export function jobsFromEvent(data: Record<string, unknown>): JobRow[] | null {
  const raw = data["jobs"]
  if (!Array.isArray(raw)) return null
  return raw.map(toJobRow)
}

// ---- data narrowing (wire values arrive as unknown) ------------------------

const str = (value: unknown): string => (typeof value === "string" ? value : "")
const num = (value: unknown): number => (typeof value === "number" ? value : 0)
const obj = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {}

// Port of blocks.py's caps: fold body 120, reason 60, nested line 120.
const PREVIEW_CHARS = 120
const REASON_CHARS = 60
const NESTED_CHARS = 120

// P3-4: the live transcript is cumulative (old rows stay after /compact),
// so it needs a bound the store never had.  Eviction is display-only: the
// session file and the transcript archive keep everything.  Shared with
// replay.ts so a reopened session lands under the same cap.
export const MAX_TRANSCRIPT_MESSAGES = 100

const RETIREMENT_NOTICE =
  "── 更早的消息已超出显示上限，仅保留最近 100 条（完整记录在存档与会话文件中）──"

// ---- immutable helpers ------------------------------------------------------

/** A compaction card is never retired: it is the visible record of a rewrite. */
function isRetirementExempt(message: ChatMessage): boolean {
  return message.parts.some((part) => part.kind === "compaction")
}

/**
 * Drop the oldest evictable messages past the cap, oldest first.
 * Exempt cards stay where they were (chronology keeps its holes honest —
 * the notice below says rows retired).  At or under the cap the array is
 * returned untouched, so Solid's keyed rendering keeps every identity.
 */
function retireOldest(messages: ChatMessage[], explain: boolean): ChatMessage[] {
  if (messages.length <= MAX_TRANSCRIPT_MESSAGES) return messages
  const evictable = messages.filter((message) => !isRetirementExempt(message))
  const dropped = Math.min(messages.length - MAX_TRANSCRIPT_MESSAGES, evictable.length)
  if (dropped <= 0) return messages
  const victims = new Set(evictable.slice(0, dropped))
  let next: ChatMessage[] = messages.filter((message) => !victims.has(message))
  if (explain) {
    // One standing notice per transcript, not one per eviction: it lives in
    // whatever message is the tail when the first eviction happens, and later
    // evictions stay silent until that message itself retires.
    const mentioned = next.some((message) =>
      message.parts.some((part) => part.kind === "notice" && part.text === RETIREMENT_NOTICE),
    )
    if (!mentioned) {
      const tail = next[next.length - 1]
      const notice = (parts: Part[]): Part[] => appendNotice(parts, RETIREMENT_NOTICE)
      if (tail !== undefined && tail.role === "assistant") {
        next = next.slice()
        next[next.length - 1] = { role: "assistant", parts: notice(tail.parts.slice()) }
      } else {
        next = [...next, { role: "assistant", parts: notice([]) }]
      }
    }
  }
  return next
}

/** Update the turn's assistant message: the tail one, or a fresh one. */
function updateAssistant(
  state: SessionState,
  update: (parts: Part[]) => Part[],
): SessionState {
  const last = state.messages[state.messages.length - 1]
  if (last?.role === "assistant") {
    const parts = update(last.parts)
    if (parts === last.parts) return state
    const messages = state.messages.slice()
    messages[messages.length - 1] = { role: "assistant", parts }
    return { ...state, messages: retireOldest(messages, true) }
  }
  const messages: ChatMessage[] = [...state.messages, { role: "assistant", parts: update([]) }]
  return { ...state, messages: retireOldest(messages, true) }
}

/** Append to the last message the user sent -- the one that drew the picture.
 *
 *  Deliberately not ``updateAssistant``: by the time a refusal arrives the turn
 *  has usually started, so the last message is an assistant one and the notice
 *  would land on text the user did not write about.
 */
function updateLastUser(
  state: SessionState,
  update: (parts: Part[]) => Part[],
): SessionState {
  for (let index = state.messages.length - 1; index >= 0; index--) {
    const message = state.messages[index]!
    if (message.role !== "user") continue
    const parts = update(message.parts)
    if (parts === message.parts) return state
    const messages = state.messages.slice()
    messages[index] = { role: "user", parts }
    return { ...state, messages }
  }
  return state
}

/** Index of the last message holding a running compaction card, if any. */
function findRunningCompaction(messages: ChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index--) {
    if (messages[index]!.parts.some((part) => part.kind === "compaction" && part.status === "running")) {
      return index
    }
  }
  return -1
}

/** Map parts, preserving the array identity when nothing changed. */
function mapParts(parts: Part[], update: (part: Part) => Part): Part[] {
  let changed = false
  const next = parts.map((part) => {
    const updated = update(part)
    if (updated !== part) changed = true
    return updated
  })
  return changed ? next : parts
}

function appendNotice(parts: Part[], text: string, tone: Tone = "muted"): Part[] {
  return [...parts, { kind: "notice", tone, text }]
}

// ---- wire-type handlers -----------------------------------------------------

const notices: Record<string, (data: Record<string, unknown>) => string> = {
  "provider.retried": (data) => {
    const reason = str(data["reason"])
    const suffix = reason ? `：${shorten(reason, REASON_CHARS)}` : ""
    return `⤷ 重试 [${str(data["kind"])}] 第 ${num(data["attempt"])} 次，等 ${num(data["delaySeconds"])}s${suffix}`
  },
  "provider.fallback": (data) => {
    const reason = str(data["reason"])
    const suffix = reason ? `：${shorten(reason, REASON_CHARS)}` : ""
    return `⤬ 降级 ${str(data["fromModel"])} → ${str(data["toModel"])}（${str(data["kind"])}）${suffix}`
  },
  "tools.warning": (data) => {
    const origins = Array.isArray(data["origins"]) ? data["origins"] : []
    const top = origins
      .filter((pair) => Array.isArray(pair) && pair[0] !== "local")
      .slice(0, 3)
      .map((pair) => (Array.isArray(pair) ? `${pair[0]}=${pair[1]}` : ""))
      .filter(Boolean)
      .join(" · ")
    const suffix = top ? `  ${top}` : ""
    // r22: the judged number is the MCP part -- the built-in tools are a fixed
    // cost the reader cannot change, so quoting it was a warning nobody could
    // act on.  The total stays visible for diagnosis.
    return `[tools] MCP schema ${num(data["mcpChars"])} 字符（阈值 ${num(data["threshold"])}，总计 ${num(data["toolsChars"])}）${suffix}`
  },
  "context.compacted": (data) =>
    `[context] 上下文已压缩 ${num(data["beforeMessages"])}→${num(data["afterMessages"])} 条消息`,
  "background.done": (data) => {
    const mark = str(data["status"]) === "completed" ? "✓" : "✗"
    let line = `${mark} 子任务完成 [${str(data["name"])}]（${str(data["status"])}）`
    const origin = str(data["originSession"])
    if (origin) line += ` · 会话 ${origin.slice(0, 12)}`
    const summary = str(data["summary"])
    if (summary) line += `：${shorten(summary, NESTED_CHARS)}`
    const ledger = str(data["ledgerPath"])
    if (ledger) line += ` · 完整回包: ${ledger}`
    // The run id is how this notice ties back to `/tasks stop <id>`, which
    // prints the same 12 chars (commands.py).  A truncated summary says so:
    // not toolmeta.truncationNote — that one marks a tail's dropped head, this
    // is a summary cut at its end (A7).
    const runId = str(data["runId"])
    if (runId) line += ` · 任务 ${runId.slice(0, 12)}`
    if (data["truncated"] === true) line += "…（摘要已截断）"
    return line
  },
}

function subagentLine(data: Record<string, unknown>): string {
  const scope = str(data["scope"])
  const inner = obj(data["inner"])
  const tag = `↳ [${scope}]`
  if (inner["kind"] === "tool" && inner["phase"] === "started") {
    const name = str(inner["toolName"])
    const target = toolTarget(name, obj(inner["params"]))
    return `${tag} ▶ ${toolVerb(name)}${target ? ` ${target}` : ""}`
  }
  if (inner["kind"] === "tool") {
    const detail = shorten(str(inner["outputPreview"]) || str(inner["error"]), NESTED_CHARS)
    const mark = inner["success"] === true ? "✓" : "✗"
    return `${tag} ${mark} ${str(inner["toolName"])}${detail ? ` ${detail}` : ""}`
  }
  return `${tag} ${shorten(str(inner["text"]), NESTED_CHARS)}`
}

function turnEndMarker(data: Record<string, unknown>): string {
  const status = str(data["status"])
  const reason = str(data["reason"])
  if (status === "timeout") return `[turn] 超时：${reason || "turn deadline exceeded"}`
  if (status === "aborted") return "[turn] 已中断（Esc），任务未收尾"
  // The loop can also stop on its token ceiling or on the operator's word; both
  // used to fall through to the empty string, so the turn ended and the
  // transcript said nothing (A8).
  if (status === "budget") return `[turn] 预算用完（${reason || "turn's token ceiling"}），输入「继续」可接上`
  if (status === "stopped") return "[turn] 已停止（你在审批时选择了停止）"
  if (data["truncatedByLength"] === true) {
    return "[turn] 回答因长度上限被截断：输入「继续」可接上"
  }
  if (status === "max_iterations") {
    return `[turn] 步数用完（${num(data["iterations"])} 步），任务未收尾：输入「继续」可接上`
  }
  if (status === "error") return `[turn] 错误：${reason}`
  return ""
}

function reduceWire(state: SessionState, event: EventMessage): SessionState {
  const data = event.data
  switch (event.type) {
    case "job.changed": {
      // The panel's own channel (D-1b): rows replace the rows, and nothing is
      // written into the transcript -- a running command is not conversation.
      const rows = jobsFromEvent(data)
      return rows === null ? state : { ...state, jobs: rows }
    }
    case "text.delta": {
      const text = str(data["text"])
      if (!text) return state
      return updateAssistant(state, (parts) => {
        const last = parts[parts.length - 1]
        if (last?.kind === "text" && last.streaming) {
          const next = parts.slice()
          next[next.length - 1] = { ...last, text: last.text + text }
          return next
        }
        return [...parts, { kind: "text", text, streaming: true }]
      })
    }
    case "message.finished": {
      const authoritative = typeof data["text"] === "string" ? (data["text"] as string) : null
      // A step's thinking goes in FRONT of that step's own text: the wire
      // carries one message per step, so the order OpenCode gets from the
      // server's part stream is ours to rebuild (A4 §2).  Whitespace is not a
      // thought, and a tool-only step has no text — neither may become a part.
      const reasoning = str(data["reasoning"]).trim()
        ? [{ kind: "reasoning", text: str(data["reasoning"]) } as Part]
        : []
      return updateAssistant(state, (parts) => {
        let next = parts
        const index = next.findLastIndex((part) => part.kind === "text" && part.streaming)
        if (index >= 0) {
          const part = next[index] as Extract<Part, { kind: "text" }>
          next = next.slice()
          next[index] = { kind: "text", text: authoritative ?? part.text, streaming: false }
          next.splice(index, 0, ...reasoning)
        } else if (authoritative) {
          next = [...next, ...reasoning, { kind: "text", text: authoritative, streaming: false }]
        } else {
          next = [...next, ...reasoning]
        }
        return next
      })
    }
    case "tool.started": {
      const callId = str(data["callId"])
      const toolName = str(data["toolName"])
      return updateAssistant(state, (parts) => [
        ...parts,
        {
          kind: "tool",
          callId,
          toolName,
          verb: toolVerb(toolName),
          target: toolTarget(toolName, obj(data["params"])),
          status: "running",
          lines: [],
          pending: "",
          linesDropped: false,
          preview: "",
          error: "",
          durationMs: 0,
        },
      ])
    }
    case "tool.chunk": {
      const callId = str(data["callId"])
      const text = str(data["text"])
      const stream = str(data["stream"]) as ToolStream
      if (!text) return state
      return updateAssistant(state, (parts) =>
        mapParts(parts, (part) => {
          if (part.kind !== "tool" || part.callId !== callId) return part
          // The pipe tag comes with the chunk: a line terminated by a stderr
          // chunk is red, and the fold in util/toolmeta keeps a line that
          // arrived in pieces from being split (and re-tagged) in half.
          const folded = foldToolChunk(
            part.lines,
            part.pending,
            text,
            stream === "stdout" || stream === "stderr" || stream === "progress" ? stream : "stdout",
          )
          return {
            ...part,
            lines: folded.lines,
            pending: folded.pending,
            linesDropped: part.linesDropped || folded.dropped,
          }
        }),
      )
    }
    case "tool.finished": {
      const callId = str(data["callId"])
      const toolName = str(data["toolName"])
      const success = data["success"] === true
      const preview = shorten(str(data["outputPreview"]), PREVIEW_CHARS)
      const error = str(data["error"])
      const durationMs = num(data["durationMs"])
      let found = false
      const updated = updateAssistant(state, (parts) =>
        mapParts(parts, (part) => {
          if (part.kind !== "tool" || part.callId !== callId) return part
          found = true
          return { ...part, status: success ? "ok" : "failed", preview, error, durationMs }
        }),
      )
      if (found) return updated
      // A missed start must not lose the result.
      return updateAssistant(updated, (parts) => [
        ...parts,
        {
          kind: "tool",
          callId,
          toolName,
          verb: toolVerb(toolName),
          target: toolTarget(toolName, {}),
          status: success ? "ok" : "failed",
          lines: [],
          pending: "",
          linesDropped: false,
          preview,
          error,
          durationMs,
        },
      ])
    }
    case "console.line": {
      const text = str(data["text"])
      if (!text) return state
      const tone: Tone = str(data["tone"]) === "error" ? "error" : "muted"
      return updateAssistant(state, (parts) => appendNotice(parts, text, tone))
    }
    case "turn.step": {
      // A command's inner turn (session.command runs /review and each /plan
      // run step as its own loop turn): settle what is streaming, keep the run
      // busy — the command's own terminal turn.finished ends it.
      const handoff = str(data["handoff"])
      return updateAssistant(state, (parts) => {
        const settled = parts.map((part) =>
          part.kind === "text" && part.streaming ? { ...part, streaming: false } : part,
        )
        return handoff ? [...settled, { kind: "text", text: handoff, streaming: false }] : settled
      })
    }
    case "plan.receipt": {
      // E2: the runner's two parent bubbles (step head + summary) travel as
      // their own event now, so the live transcript matches what a reload
      // would show -- they used to appear only after reopening the session.
      const prompt = str(data["prompt"])
      const summary = str(data["summary"])
      if (!prompt && !summary) return state
      return {
        ...state,
        messages: retireOldest(
          [
            ...state.messages,
            { role: "user" as const, parts: [{ kind: "text" as const, text: prompt, streaming: false }] },
            {
              role: "assistant" as const,
              parts: [{ kind: "text" as const, text: summary, streaming: false }],
            },
          ],
          false,
        ),
      }
    }
    case "plan.updated":
      return applyPlanUpdate(state, data)
    case "ledger.updated": {
      const items = Array.isArray(data["items"]) ? data["items"] : []
      const lines: string[] = []
      for (const raw of items) {
        const item = obj(raw)
        lines.push(`[${str(item["level"])}] ${str(item["question"])}`)
        const evidence = Array.isArray(item["evidence"]) ? item["evidence"] : []
        for (const bit of evidence) lines.push(`  · ${str(bit)}`)
      }
      return updateAssistant(state, (parts) => appendNotice(parts, lines.join("\n")))
    }
    case "subagent.event":
      return updateAssistant(state, (parts) => appendNotice(parts, subagentLine(data)))
    case "compaction.started": {
      const reason = str(data["reason"]) || "auto"
      return updateAssistant(state, (parts) => [
        ...parts,
        {
          kind: "compaction",
          status: "running",
          reason,
          text: "",
          beforeMessages: 0,
          afterMessages: 0,
          inputTokens: 0,
          outputTokens: 0,
          totalTokens: 0,
          error: "",
        },
      ])
    }
    case "compaction.delta": {
      const text = str(data["text"])
      if (!text) return state
      // The running card is found wherever it is, not just at the tail: the
      // user may have typed (a queued row) mid-compaction, and the wipe that
      // used to hide the misplacement is gone (cumulative transcript).
      const index = findRunningCompaction(state.messages)
      if (index < 0) return state
      const messages = state.messages.slice()
      const message = messages[index]!
      messages[index] = {
        ...message,
        parts: mapParts(message.parts, (part) =>
          part.kind === "compaction" && part.status === "running"
            ? { ...part, text: part.text + text }
            : part,
        ),
      }
      return { ...state, messages: retireOldest(messages, true) }
    }
    case "compaction.ended": {
      const authoritative = str(data["text"])
      const done = {
        kind: "compaction",
        status: "done",
        reason: str(data["reason"]) || "auto",
        text: authoritative,
        beforeMessages: num(data["beforeMessages"]),
        afterMessages: num(data["afterMessages"]),
        inputTokens: num(data["inputTokens"]),
        outputTokens: num(data["outputTokens"]),
        totalTokens: num(data["totalTokens"]),
        error: "",
      } as Part
      const withoutLegacy = (parts: Part[]): Part[] =>
        parts.filter(
          (part) =>
            part.kind !== "notice" ||
            !String((part as { text: string }).text).startsWith("[context] 上下文已压缩"),
        )
      // The legacy one-liner is swallowed once the lifecycle row exists:
      // two rows for one compaction would read as two compactions.
      const index = findRunningCompaction(state.messages)
      if (index < 0) {
        return updateAssistant(state, (parts) => withoutLegacy([...parts, done]))
      }
      const messages = state.messages.slice()
      const message = messages[index]!
      messages[index] = {
        ...message,
        parts: withoutLegacy(
          message.parts.map((part) =>
            part.kind === "compaction" && part.status === "running" ? done : part,
          ),
        ),
      }
      return { ...state, messages: retireOldest(messages, true) }
    }
    case "compaction.failed": {
      const error = str(data["error"]) || "unknown error"
      const failed = {
        kind: "compaction",
        status: "failed",
        reason: str(data["reason"]) || "auto",
        text: "",
        beforeMessages: 0,
        afterMessages: 0,
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        error,
      } as Part
      // The failed card IS the red error row (✗ 压缩失败：{error}); appending a
      // notice with the same text doubled the report (measured live: two
      // identical ✗ lines for one failure).
      const index = findRunningCompaction(state.messages)
      if (index < 0) {
        return updateAssistant(state, (parts) => [...parts, failed])
      }
      const messages = state.messages.slice()
      const message = messages[index]!
      messages[index] = {
        ...message,
        parts: message.parts.map((part) =>
          part.kind === "compaction" && part.status === "running" ? failed : part,
        ),
      }
      return { ...state, messages: retireOldest(messages, true) }
    }
    case "compaction.skipped": {
      // Nothing worth summarising (single exchange / nothing since the
      // checkpoint): a muted card, not a failure.  No billing happened.
      const skipped = {
        kind: "compaction",
        status: "skipped",
        reason: str(data["reason"]) || "auto",
        text: "",
        beforeMessages: 0,
        afterMessages: 0,
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        error: "",
      } as Part
      const index = findRunningCompaction(state.messages)
      if (index < 0) {
        return updateAssistant(state, (parts) => [...parts, skipped])
      }
      const messages = state.messages.slice()
      const message = messages[index]!
      messages[index] = {
        ...message,
        parts: message.parts.map((part) =>
          part.kind === "compaction" && part.status === "running" ? skipped : part,
        ),
      }
      return { ...state, messages: retireOldest(messages, true) }
    }
    case "turn.finished": {
      // D7: an ending settles the turn it names.  Home's /plan prelude and the
      // prompt submitted right after it are two turns; the command's ending
      // must not free a composer the prompt already took.
      const ending = num(data["turnId"])
      if (ending !== 0 && state.turnId !== ending) return state
      const handoff = str(data["handoff"])
      // stage 4 r2: the plan guard's terminal line (a muted notice, not the
      // red turn-end marker -- the turn itself completed fine).
      const note = str(data["note"])
      const marker = turnEndMarker(data)
      const updated = updateAssistant(state, (parts) => {
        let next = mapParts(parts, (part) =>
          part.kind === "text" && part.streaming ? { ...part, streaming: false } : part,
        )
        if (handoff) next = [...next, { kind: "text", text: handoff, streaming: false }]
        if (note) next = appendNotice(next, note)
        if (marker) next = [...next, { kind: "turn-end", tone: "error", text: marker }]
        return next
      })
      return { ...updated, busy: false, turnId: undefined }
    }
    default: {
      const build = notices[event.type]
      if (!build) return state
      return updateAssistant(state, (parts) => appendNotice(parts, build(data)))
    }
  }
}

/** 20 §批次2 + R05: paint one plan card from one plan payload.
 *
 * The live ``plan.updated`` event and a reopened session's recovered task use
 * this exact reducer — one card shape, so reload and live cannot drift.
 * An empty goal+items is the plan being dropped ("(无任务项)" must leave).
 */
export function applyPlanUpdate(
  state: SessionState,
  data: Record<string, unknown>,
): SessionState {
  const goal = str(data["goal"])
  const items: PlanItem[] = Array.isArray(data["items"])
    ? data["items"].map((raw) => {
        const item = obj(raw)
        // D5: read the same two keys the review card reads (there: numbers,
        // here: the plan's own string ids). Malformed values are dropped,
        // never guessed -- mirrors planReviewItems in util/ask.
        const rawDeps = item["dependsOn"]
        return {
          id: str(item["id"]),
          content: str(item["content"]),
          status: str(item["status"]),
          verify: str(item["verify"]),
          dependsOn: Array.isArray(rawDeps)
            ? rawDeps.filter((d): d is string => typeof d === "string")
            : [],
        }
      })
    : []
  const next = updateAssistant(state, (parts) => {
    const index = parts.findIndex((part) => part.kind === "plan")
    if (!goal && items.length === 0) {
      return index < 0 ? parts : parts.filter((part) => part.kind !== "plan")
    }
    const plan: Part = { kind: "plan", goal, items }
    if (index < 0) return [...parts, plan]
    const next = parts.slice()
    next[index] = plan
    return next
  })
  // G2: the same payload feeds the sidebar's goal line.  Empty goal+items
  // is the task being dropped, so the status clears with the card.
  const status: GoalStatus | null =
    goal || items.length > 0
      ? {
          goal,
          armed: data["armed"] === true,
          roundsRun: num(data["roundsRun"]),
          tokensUsed: num(data["tokensUsed"]),
          phase: str(data["goalPhase"]),
        }
      : null
  return { ...next, goal: status }
}

export function reduce(state: SessionState, action: Action): SessionState {
  switch (action.kind) {
    case "submitted":
      return {
        ...state,
        messages: retireOldest(
          [
            ...state.messages,
            {
              role: "user",
              parts: [
                // An image-only message has no text part at all (r17/T5).
                ...(action.text
                  ? [{ kind: "text", text: action.text, streaming: false } as const]
                  : []),
                ...(action.images ?? []).map((image) => ({ kind: "image", name: image.name }) as const),
              ],
            },
          ],
          false,
        ),
        busy: true,
      }
    case "resumed":
      return { ...state, busy: true }
    case "command-started":
      return { ...state, busy: true }
    case "turn-started":
      // Identity only: the busy flag already went up on the way in, and a
      // steer's "submitted" must not detach the running turn (D7).
      return { ...state, turnId: action.turnId }
    case "queued":
      // The user message lands now (the retired Textual UI appended at submit
      // too); the turn itself starts when the running one ends.
      return {
        ...state,
        messages: retireOldest(
          [
            ...state.messages,
            { role: "user", parts: [{ kind: "text", text: action.text, streaming: false }] },
          ],
          false,
        ),
        queued: [...state.queued, { id: action.id, text: action.text }],
      }
    case "queued-dispatched":
      return { ...state, busy: true, queued: state.queued.slice(1) }
    case "queued-loaded":
      return { ...state, queued: action.rows }
    case "queued-edited":
      return {
        ...state,
        queued: state.queued.map((row) =>
          row.id === action.id ? { ...row, text: action.text } : row,
        ),
      }
    case "queued-deleted":
      return { ...state, queued: state.queued.filter((row) => row.id !== action.id) }
    case "jobs":
      return { ...state, jobs: action.jobs }
    case "submit-failed": {
      const quiet = { ...state, busy: false }
      return {
        ...quiet,
        messages: updateAssistant(quiet, (parts) =>
          appendNotice(parts, `[ipc] ${action.reason}`, "error"),
        ).messages,
      }
    }
    case "attachment-rejected": {
      if (action.items.length === 0) return state
      const line = action.items
        .map((item) => `[附件未附加] ${item.name}：${item.reason}`)
        .join("；")
      return updateLastUser(state, (parts) => appendNotice(parts, line, "error"))
    }
    case "wire":
      return reduceWire(state, action.event)
  }
}
