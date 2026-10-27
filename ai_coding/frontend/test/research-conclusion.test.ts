/**
 * TDD (23): the research conclusion must be visible.
 *
 * A plan-mode turn that closes with the research exit (`finish_research`)
 * produces a conclusion -- a report the user asked for.  It used to ride the
 * tool-result channel, which is the one lossy channel in the system (500
 * chars in the event, 120 on the screen) and cannot be opened either (the
 * tool streams no chunks).  Measured on a real session: a 4116-char
 * conclusion reached the user as 120 chars.
 *
 * So the conclusion gets its own channel and is drawn as markdown prose.
 * These tests pin the two halves of that: the live path (a wire event) and
 * the reload path (the persisted tool message), which must agree.
 */

import { describe, expect, test } from "bun:test"
import type { Action, SessionState } from "../src/state/session"
import { initialState, reduce } from "../src/state/session"
import type { WireMessage } from "../src/state/replay"
import { messagesToState } from "../src/state/replay"

const wire = (type: string, data: Record<string, unknown> = {}): Action => ({
  kind: "wire",
  event: { type, data },
})

const feed = (state: SessionState, ...actions: Action[]): SessionState =>
  actions.reduce(reduce, state)

const lastAssistant = (state: SessionState) => state.messages[state.messages.length - 1]!

// A conclusion longer than the 120-char preview the tool card can show.
// The tool strips the summary before it returns it, so the wire carries the
// trimmed text -- and the reload path trims the envelope the same way.
const CONCLUSION = ("## 结论\n\n" + "计划模式把意图、条目、证明命令分开建模。\n".repeat(120)).trim()

// The real envelope (ai_coding/domain/run.py::wrap_tool_output).
const envelope = (body: string, ok = true): string =>
  `<tool_output tool="finish_research"${ok ? "" : ' ok="false"'}>\n<data>\n{"research_finished":true}\n</data>\n${body}\n</tool_output>`

describe("the research conclusion (live)", () => {
  test("it is drawn as prose in the turn's assistant message", () => {
    const state = feed(
      initialState(),
      wire("text.delta", { text: "let me conclude" }),
      wire("tool.started", { callId: "c1", toolName: "finish_research", params: {} }),
      wire("tool.finished", { callId: "c1", toolName: "finish_research", success: true, outputPreview: "…" }),
      wire("research.concluded", { summary: CONCLUSION }),
    )
    const parts = lastAssistant(state).parts
    const texts = parts.filter((part) => part.kind === "text")
    expect(texts.length).toBe(2)
    expect(texts[1]).toEqual({ kind: "text", text: CONCLUSION, streaming: false })
  })

  test("the whole conclusion is on screen, not a 120-char preview", () => {
    const state = feed(
      initialState(),
      wire("research.concluded", { summary: CONCLUSION }),
    )
    const part = lastAssistant(state).parts[0]!
    expect(part.kind).toBe("text")
    if (part.kind !== "text") throw new Error("not a text part")
    expect(part.text).toBe(CONCLUSION)
    expect(part.text.length).toBeGreaterThan(500)
  })
})

describe("the research conclusion (reload)", () => {
  const messages: WireMessage[] = [
    { role: "user", content: "调研这个仓库" },
    {
      role: "assistant",
      content: null,
      toolCalls: [{ id: "c1", name: "finish_research", arguments: JSON.stringify({ summary: CONCLUSION }) }],
    },
    { role: "tool", content: envelope(CONCLUSION), toolCallId: "c1", toolName: "finish_research" },
  ]

  test("a reloaded conclusion is prose, whole", () => {
    const state = messagesToState(messages)
    const texts = lastAssistant(state).parts.filter((part) => part.kind === "text")
    expect(texts).toHaveLength(1)
    expect(texts[0]).toEqual({ kind: "text", text: CONCLUSION, streaming: false })
  })

  test("live and reload draw the same thing", () => {
    const live = feed(
      initialState(),
      wire("tool.started", { callId: "c1", toolName: "finish_research", params: {} }),
      wire("tool.finished", { callId: "c1", toolName: "finish_research", success: true, outputPreview: "…" }),
      wire("research.concluded", { summary: CONCLUSION }),
    )
    const reloaded = messagesToState(messages)
    const liveText = lastAssistant(live).parts.find((p) => p.kind === "text")
    const reloadText = lastAssistant(reloaded).parts.find((p) => p.kind === "text")
    expect(liveText).toEqual(reloadText)
  })

  test("a refused conclusion is not a conclusion", () => {
    const state = messagesToState([
      {
        role: "assistant",
        content: null,
        toolCalls: [{ id: "c1", name: "finish_research", arguments: "{}" }],
      },
      {
        role: "tool",
        content: envelope("该请求要实现", false),
        toolCallId: "c1",
        toolName: "finish_research",
      },
    ])
    const texts = lastAssistant(state).parts.filter((part) => part.kind === "text")
    expect(texts).toHaveLength(0)
  })
})
