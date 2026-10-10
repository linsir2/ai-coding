/**
 * TDD (M3): the history replay converter (state/replay.ts).
 *
 * A loaded session is a flat list of persisted messages; the transcript wants
 * the same parts model live events produce.  The rules are a port of the old
 * Textual replay (ui/blocks.py, now retired: _strip_envelope,
 * tool_outcome_from_message, tool_card_identity), pinned here so a reopened
 * session reads like the turn that produced it.
 */

import { describe, expect, test } from "bun:test"
import type { WireMessage } from "../src/state/replay"
import { messagesToState } from "../src/state/replay"

// The real envelope (ai_coding/domain/run.py::wrap_tool_output): the <data>
// block is on its own lines, so _strip_envelope's exact-line checks apply.
const envelope = (body: string, ok = true): string =>
  `<tool_output tool="bash"${ok ? "" : ' ok="false"'}>\n<data>\n{"x":1}\n</data>\n${body}\n</tool_output>`

describe("messagesToState", () => {
  test("user and assistant prose become their own messages", () => {
    const state = messagesToState([
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ])
    expect(state.messages).toEqual([
      { role: "user", parts: [{ kind: "text", text: "hi", streaming: false }] },
      { role: "assistant", parts: [{ kind: "text", text: "hello", streaming: false }] },
    ])
    expect(state.busy).toBe(false)
  })

  test("a multi-step turn becomes ONE assistant message", () => {
    const messages: WireMessage[] = [
      { role: "user", content: "q" },
      { role: "assistant", content: "step one", toolCalls: [{ id: "c1", name: "bash", arguments: '{"command":"ls"}' }] },
      { role: "tool", content: envelope("a.txt"), toolCallId: "c1", toolName: "bash" },
      { role: "assistant", content: "final" },
    ]
    const state = messagesToState(messages)
    expect(state.messages).toHaveLength(2)
    const assistant = state.messages[1]!
    expect(assistant.role).toBe("assistant")
    expect(assistant.parts.map((p) => p.kind)).toEqual(["text", "tool", "text"])
  })

  test("a matching tool result settles the card with the stripped output", () => {
    const state = messagesToState([
      { role: "assistant", content: null, toolCalls: [{ id: "c1", name: "read", arguments: '{"path":"/a"}' }] },
      { role: "tool", content: envelope("line one\nline two"), toolCallId: "c1", toolName: "read" },
    ])
    const part = state.messages[0]!.parts[0]!
    expect(part).toMatchObject({ kind: "tool", status: "ok", preview: "line one line two" })
  })

  test("a failed tool result is red and carries the text as its error", () => {
    const state = messagesToState([
      { role: "assistant", content: null, toolCalls: [{ id: "c1", name: "bash", arguments: "{}" }] },
      { role: "tool", content: envelope("boom", false), toolCallId: "c1", toolName: "bash" },
    ])
    const part = state.messages[0]!.parts[0]!
    expect(part).toMatchObject({ kind: "tool", status: "failed", error: "boom", preview: "" })
  })

  test("tool cards derive their verb and target from the call arguments", () => {
    const state = messagesToState([
      {
        role: "assistant",
        content: null,
        toolCalls: [{ id: "c1", name: "bash", arguments: '{"command":"ls -la"}' }],
      },
    ])
    expect(state.messages[0]!.parts[0]).toMatchObject({
      kind: "tool",
      verb: "运行命令",
      target: "ls -la",
      status: "running",
    })
  })

  // A4 T5: the replayed order must match the live stream's exactly (A4 §2 —
  // one contract, two implementation points), so reasoning leads its text here
  // just as it does in the reducer.
  test("reasoning travels in front of the text it belongs to", () => {
    const state = messagesToState([{ role: "assistant", content: "answer", reasoning: "because" }])
    expect(state.messages[0]!.parts).toEqual([
      { kind: "reasoning", text: "because" },
      { kind: "text", text: "answer", streaming: false },
    ])
  })

  test("malformed call arguments do not throw and yield no target", () => {
    const state = messagesToState([
      { role: "assistant", content: null, toolCalls: [{ id: "c1", name: "bash", arguments: "not json" }] },
    ])
    expect(state.messages[0]!.parts[0]).toMatchObject({ kind: "tool", target: "" })
  })

  test("orphan tool messages are ignored and empty assistants are dropped", () => {
    const state = messagesToState([
      { role: "user", content: "q" },
      { role: "tool", content: envelope("nobody asked"), toolCallId: "ghost", toolName: "bash" },
      { role: "assistant", content: "" },
    ])
    expect(state.messages).toEqual([
      { role: "user", parts: [{ kind: "text", text: "q", streaming: false }] },
    ])
  })
})
