/**
 * Session state for the transcript (M2) — a pure reducer over wire events.
 *
 * The reducer is the single place that turns wire data into what the UI
 * shows; every string here is pinned by test/reducer.test.ts and ports the
 * old transcript's wording (ai_coding/ui/blocks.py) plus the four approved
 * M2 deltas.  Components stay thin: they render parts, they never derive.
 *
 * Updates are immutable but referentially stable: untouched messages and
 * parts keep their identity so Solid's keyed rendering only touches what
 * changed (one growing text part must not remount the whole transcript).
 */

import type { EventMessage } from "../ipc"
import { shorten, toolTarget, toolVerb } from "../util/toolmeta"

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
      tail: string
      preview: string
      error: string
    }
  | { kind: "plan"; title: string; body: string }
  | { kind: "notice"; tone: Tone; text: string }
  | { kind: "turn-end"; tone: "error"; text: string }

export interface ChatMessage {
  role: "user" | "assistant"
  parts: Part[]
}

export interface SessionState {
  messages: ChatMessage[]
  /** True from submit until the turn's finish event; gates the prompt. */
  busy: boolean
}

export type Action =
  | { kind: "submitted"; text: string }
  | { kind: "submit-failed"; reason: string }
  | { kind: "wire"; event: EventMessage }

export function initialState(): SessionState {
  return { messages: [], busy: false }
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
const TOOL_TAIL_CHARS = 4000

// ---- immutable helpers ------------------------------------------------------

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
    return { ...state, messages }
  }
  return { ...state, messages: [...state.messages, { role: "assistant", parts: update([]) }] }
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
  "approval.requested": (data) => {
    const reason = str(data["reason"])
    const suffix = reason ? `：${shorten(reason, REASON_CHARS)}` : ""
    return `⚠ 需审批 [${str(data["toolName"])}] 已自动批准（审批面板 M5）${suffix}`
  },
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
      .slice(0, 3)
      .map((pair) => (Array.isArray(pair) ? `${pair[0]}=${pair[1]}` : ""))
      .filter(Boolean)
      .join(" · ")
    const suffix = top ? `  ${top}` : ""
    return `[tools] ${num(data["toolsChars"])} 字符（阈值 ${num(data["threshold"])}）${suffix}`
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
  if (status === "timeout") return `[turn] timed out: ${str(data["reason"])}`
  if (status === "aborted") return "[turn] 已中断（Esc），任务未收尾"
  if (data["truncatedByLength"] === true) {
    return "[turn] 回答因长度上限被截断：输入「继续」可接上"
  }
  if (status === "max_iterations") {
    return `[turn] 步数用完（${num(data["iterations"])} 步），任务未收尾：输入「继续」可接上`
  }
  if (status === "error") return `[turn] 错误：${str(data["reason"])}`
  return ""
}

function reduceWire(state: SessionState, event: EventMessage): SessionState {
  const data = event.data
  switch (event.type) {
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
      const reasoning = str(data["reasoning"])
      return updateAssistant(state, (parts) => {
        let next = parts
        const index = next.findLastIndex((part) => part.kind === "text" && part.streaming)
        if (index >= 0) {
          const part = next[index] as Extract<Part, { kind: "text" }>
          next = next.slice()
          next[index] = { kind: "text", text: authoritative ?? part.text, streaming: false }
        } else if (authoritative !== null) {
          next = [...next, { kind: "text", text: authoritative, streaming: false }]
        }
        if (reasoning) next = [...next, { kind: "reasoning", text: reasoning }]
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
          tail: "",
          preview: "",
          error: "",
        },
      ])
    }
    case "tool.chunk": {
      const callId = str(data["callId"])
      const text = str(data["text"])
      if (!text) return state
      return updateAssistant(state, (parts) =>
        mapParts(parts, (part) =>
          part.kind === "tool" && part.callId === callId
            ? { ...part, tail: (part.tail + text).slice(-TOOL_TAIL_CHARS) }
            : part,
        ),
      )
    }
    case "tool.finished": {
      const callId = str(data["callId"])
      const toolName = str(data["toolName"])
      const success = data["success"] === true
      const preview = shorten(str(data["outputPreview"]), PREVIEW_CHARS)
      const error = str(data["error"])
      let found = false
      const updated = updateAssistant(state, (parts) =>
        mapParts(parts, (part) => {
          if (part.kind !== "tool" || part.callId !== callId) return part
          found = true
          return { ...part, status: success ? "ok" : "failed", preview, error }
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
          tail: "",
          preview,
          error,
        },
      ])
    }
    case "plan.updated": {
      const plan: Part = {
        kind: "plan",
        title: `计划 ${num(data["done"])}/${num(data["total"])}`,
        body: str(data["display"]),
      }
      return updateAssistant(state, (parts) => {
        const index = parts.findIndex((part) => part.kind === "plan")
        if (index < 0) return [...parts, plan]
        const next = parts.slice()
        next[index] = plan
        return next
      })
    }
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
    case "turn.finished": {
      const handoff = str(data["handoff"])
      const marker = turnEndMarker(data)
      const updated = updateAssistant(state, (parts) => {
        let next = mapParts(parts, (part) =>
          part.kind === "text" && part.streaming ? { ...part, streaming: false } : part,
        )
        if (handoff) next = [...next, { kind: "text", text: handoff, streaming: false }]
        if (marker) next = [...next, { kind: "turn-end", tone: "error", text: marker }]
        return next
      })
      return { ...updated, busy: false }
    }
    default: {
      const build = notices[event.type]
      if (!build) return state
      return updateAssistant(state, (parts) => appendNotice(parts, build(data)))
    }
  }
}

export function reduce(state: SessionState, action: Action): SessionState {
  switch (action.kind) {
    case "submitted":
      return {
        messages: [
          ...state.messages,
          { role: "user", parts: [{ kind: "text", text: action.text, streaming: false }] },
        ],
        busy: true,
      }
    case "submit-failed":
      return {
        busy: false,
        messages: updateAssistant(
          { ...state, busy: false },
          (parts) => appendNotice(parts, `[ipc] ${action.reason}`, "error"),
        ).messages,
      }
    case "wire":
      return reduceWire(state, action.event)
  }
}
