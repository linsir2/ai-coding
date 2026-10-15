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
import type { RecoveryFacts, WireMessage } from "../src/state/replay"
import type { Part } from "../src/state/session"
import { loadSession, messagesToState, recoveryNotice } from "../src/state/replay"

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

  // P3-2 refactor follow-up: the checkpoint note is stored as a user-role
  // message (so the model reads it as history and replies pair correctly),
  // but it is model-generated markdown.  Replayed as a plain user row the
  // `##` sections show literally (screenshot: unrendered summary replacing
  // the transcript after /compact).  It replays as assistant prose instead —
  // the same part the live stream renders markdown through — with zero view
  // changes.
  test("a checkpoint note replays as assistant prose, not a plain user row", () => {
    const body = "[以下为早期对话摘要]\n## 目标\n继续修压缩"
    const state = messagesToState([{ role: "user", content: body }])
    expect(state.messages).toEqual([
      { role: "assistant", parts: [{ kind: "text", text: body, streaming: false }] },
    ])
  })

  test("a user message merely mentioning the mark stays a user row", () => {
    const state = messagesToState([{ role: "user", content: "看到[以下为早期对话摘要]这行字了吗" }])
    expect(state.messages).toEqual([
      {
        role: "user",
        parts: [{ kind: "text", text: "看到[以下为早期对话摘要]这行字了吗", streaming: false }],
      },
    ])
  })

  test("a replayed checkpoint followed by new turns keeps turn shape", () => {
    const state = messagesToState([
      { role: "user", content: "[以下为早期对话摘要]\n## 目标\n做完" },
      { role: "user", content: "继续" },
      { role: "assistant", content: "好" },
    ])
    expect(state.messages.map((m) => m.role)).toEqual(["assistant", "user", "assistant"])
    expect(state.messages[0]!.parts).toEqual([
      { kind: "text", text: "[以下为早期对话摘要]\n## 目标\n做完", streaming: false },
    ])
  })
})

describe("replay retirement (P3-4)", () => {
  test("a reopened transcript lands under the same cap, newest kept", async () => {
    const { MAX_TRANSCRIPT_MESSAGES } = await import("../src/state/session")
    const messages: WireMessage[] = []
    for (let i = 0; i < MAX_TRANSCRIPT_MESSAGES + 50; i++) {
      messages.push({ role: "user", content: `old-${i}` })
    }
    const state = messagesToState(messages)
    expect(state.messages).toHaveLength(MAX_TRANSCRIPT_MESSAGES)
    expect(state.messages[0]).toEqual({
      role: "user",
      parts: [{ kind: "text", text: "old-50", streaming: false }],
    })
  })
})


// ---- P5: the recovery facts become a notice ---------------------------------
//
// The channels already exist -- a ``notice`` part is already rendered, and
// ``session.load`` already returns a payload the client awaits -- so the whole
// change is one pure function plus one unshift.

const asNotice = (part: Part | null): Extract<Part, { kind: "notice" }> => {
  expect(part?.kind).toBe("notice")
  return part as Extract<Part, { kind: "notice" }>
}

describe("recoveryNotice", () => {
  test("a recovered crash names the turn and says what to do", () => {
    const note = asNotice(recoveryNotice({ recoveredInterrupted: true, openTurn: 3 }))
    expect(note.tone).toBe("error")
    expect(note.text).toContain("3")
    expect(note.text).toContain("未完成")
  })

  test("a pending approval names the tool and its target", () => {
    const note = asNotice(
      recoveryNotice({ pendingApproval: { toolName: "bash", params: { command: "echo hi" } } }),
    )
    expect(note.tone).toBe("error")
    expect(note.text).toContain("bash")
    expect(note.text).toContain("echo hi")
  })

  test("both facts produce one notice, not two overlapping stories", () => {
    const note = asNotice(
      recoveryNotice({
        recoveredInterrupted: true,
        openTurn: 1,
        pendingApproval: { toolName: "bash", params: { command: "x" } },
      }),
    )
    expect(note.text).toContain("bash")
  })

  test("a clean session gets nothing at all", () => {
    expect(recoveryNotice({})).toBeNull()
    expect(
      recoveryNotice({ recoveredInterrupted: false, openTurn: 0, pendingApproval: {} }),
    ).toBeNull()
  })
})

describe("loadSession", () => {
  const client = (payload: object) =>
    ({ request: async () => payload }) as unknown as Parameters<typeof loadSession>[0]

  test("puts the notice in front of the transcript", async () => {
    const state = await loadSession(
      client({
        messages: [{ role: "user", content: "hi" }],
        recoveredInterrupted: true,
        openTurn: 2,
      }),
      "s1",
    )
    expect(state.messages[0]!.parts[0]!.kind).toBe("notice")
    expect(state.messages[1]!.parts[0]).toEqual({
      kind: "text",
      text: "hi",
      streaming: false,
    })
  })

  test("a clean session replays exactly as before", async () => {
    const state = await loadSession(client({ messages: [{ role: "user", content: "hi" }] }), "s1")
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0]!.parts[0]!.kind).toBe("text")
  })
})
