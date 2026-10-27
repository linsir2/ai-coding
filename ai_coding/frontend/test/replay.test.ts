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
import { loadSession, messagesToState, recoveryNotice, resumeSession } from "../src/state/replay"

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

  test("a loaded task rebuilds the plan card (R05)", async () => {
    const state = await loadSession(
      client({
        messages: [{ role: "assistant", content: "done" }],
        task: {
          goal: "把卡带回来",
          items: [{ id: "1", content: "修回读", status: "completed" }],
          armed: true,
        },
      }),
      "s1",
    )
    const card = state.messages.flatMap((m) => m.parts).find((p) => p.kind === "plan")
    expect(card).toMatchObject({
      kind: "plan",
      goal: "把卡带回来",
      items: [{ id: "1", content: "修回读", status: "completed" }],
    })
    expect(state.goal?.armed).toBe(true)
  })

  test("a clean session replays exactly as before", async () => {
    const state = await loadSession(client({ messages: [{ role: "user", content: "hi" }] }), "s1")
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0]!.parts[0]!.kind).toBe("text")
  })
})

describe("resume", () => {
  test("a recoverable notice carries the resume action and its key", () => {
    const crashed = asNotice(recoveryNotice({ recoveredInterrupted: true, openTurn: 3 }))
    expect(crashed.action).toBe("resume")
    expect(crashed.text).toContain("ctrl+x r")

    const pending = asNotice(
      recoveryNotice({
        recoveredInterrupted: true,
        openTurn: 1,
        pendingApproval: { toolName: "bash", params: { command: "echo hi" } },
      }),
    )
    expect(pending.action).toBe("resume")
    expect(pending.text).toContain("ctrl+x r")
  })

  test("a marker without an open turn promises no key (08/addendum-2)", () => {
    // Crashed before the first checkpoint: the user message never reached the
    // file, so the backend would refuse -- the notice informs, it does not
    // offer what cannot run.
    const note = asNotice(
      recoveryNotice({ pendingApproval: { toolName: "bash", params: { command: "echo hi" } } }),
    )
    expect(note.action).toBeUndefined()
    expect(note.text).not.toContain("ctrl+x r")
  })

  test("resumeSession asks the backend to continue without a prompt", async () => {
    const seen: Array<{ method: string; params: unknown }> = []
    const client = {
      request: (method: string, params: unknown) => {
        seen.push({ method, params })
        return Promise.resolve({ turnId: 7, sessionId: "s" })
      },
    } as unknown as Parameters<typeof resumeSession>[0]
    const ack = await resumeSession(client, "s")
    expect(seen).toEqual([{ method: "session.resume", params: { id: "s" } }])
    expect(ack.turnId).toBe(7)
  })
})

describe("[steering] replay (steering 前端化批次)", () => {
  test("a mid-turn line replays as the user's own words", () => {
    // The prefix is the harness speaking TO THE MODEL; the operator typed the
    // rest, and a reloaded transcript must read it that way.
    const state = messagesToState([
      { role: "user", content: "[steering] 先看测试" },
      { role: "assistant", content: "好" },
    ])
    expect(state.messages[0]).toEqual({
      role: "user",
      parts: [{ kind: "text", text: "先看测试", streaming: false }],
    })
  })

  test("a background fact replays as a notice, never as the user's words", () => {
    // P0-2: "[notice] " is the harness speaking ABOUT the run. The steering
    // branch below would render it as something the operator typed, which is
    // a lie in the permanent record -- the same reason it strips that marker.
    const state = messagesToState([
      { role: "user", content: "[notice] [子任务完成] probe（abc123）：跑完了" },
      { role: "assistant", content: "收到" },
    ])
    // Replay folds everything after a user message into one assistant message,
    // so the fact and the answer that follows share it -- which is the point:
    // it renders on the assistant's side of the transcript.
    expect(state.messages).toHaveLength(1)
    const parts = state.messages[0]?.parts ?? []
    expect(state.messages[0]?.role).toBe("assistant")
    expect(parts[0]).toEqual({
      kind: "notice",
      tone: "muted",
      text: "[子任务完成] probe（abc123）：跑完了",
    })
    expect(parts.some((part) => part.kind === "text" && String(part.text).includes("收到"))).toBe(
      true,
    )
  })

  test("text that merely starts with a bracket is untouched", () => {
    const state = messagesToState([{ role: "user", content: "[note] 保持" }])
    expect(state.messages[0]?.parts).toEqual([
      { kind: "text", text: "[note] 保持", streaming: false },
    ])
  })
})

describe("[plan] replay (计划模式归位批次)", () => {
  test("the harness's planning line is skipped, the operator's words stay", () => {
    // The planning round writes two messages: what the operator said, and
    // the instruction the harness gave the model.  The transcript shows the
    // first and skips the second -- a page of instruction in the operator's
    // column would read as if they had said it.
    //
    // The instruction is *stamped* (M2 T3): the mark is the class, not a
    // prefix to recognise.  The replay used to look for the "[plan] "
    // prefix; now the synthetic branch skips it, like every other harness
    // line.
    const state = messagesToState([
      { role: "user", content: "请你调研这个分支" },
      {
        role: "user",
        content: "[plan] Create a detailed implementation plan for: 请你调研这个分支",
        synthetic: true,
      },
      { role: "assistant", content: "好" },
    ])
    const texts = state.messages.flatMap((m) =>
      m.parts.map((p) => ("text" in p ? p.text : "")),
    )
    expect(texts).toContain("请你调研这个分支")
    expect(texts.some((t) => t.includes("Create a detailed implementation plan"))).toBe(false)
  })

  test("the other harness lines keep their own rendering", () => {
    // The mark is a prefix, not a class: "[notice] " still reads as a notice
    // and "[steering] " still reads as the operator's own mid-turn words.
    const notice = messagesToState([
      { role: "user", content: "[notice] [子任务完成] probe：跑完了" },
    ])
    expect(notice.messages).toHaveLength(1)
    expect(notice.messages[0]?.role).toBe("assistant")

    const steering = messagesToState([
      { role: "user", content: "[steering] 先看测试" },
    ])
    expect(steering.messages[0]).toEqual({
      role: "user",
      parts: [{ kind: "text", text: "先看测试", streaming: false }],
    })
  })
})

describe("synthetic bookkeeping (D6)", () => {
  test("a stamped step marker never replays as the operator's words", () => {
    // The plan runner writes the step head into the parent transcript so
    // /resume can rebuild the timeline; it is the harness speaking about the
    // run, and the live card no longer draws it either.
    const state = messagesToState([
      { role: "user", content: "实现某功能" },
      { role: "user", content: "[plan] 只做这一步，不要做别的：实现某功能", synthetic: true },
      { role: "assistant", content: "[plan] 步骤 1 完成：实现某功能" },
    ])
    expect(state.messages.map((message) => message.role)).toEqual(["user", "assistant"])
    const texts = state.messages.flatMap((message) =>
      message.parts.map((part) => ("text" in part ? part.text : "")),
    )
    expect(texts.some((text) => text.includes("只做这一步"))).toBe(false)
  })

  test("a stamped nudge without a mark is skipped too", () => {
    // The guard is the stamp, not a prefix: closing prompts and decision
    // nudges were already synthetic and still replayed as user bubbles.
    const state = messagesToState([
      { role: "user", content: "收个尾，先汇报再结束", synthetic: true },
      { role: "assistant", content: "好" },
    ])
    expect(state.messages.map((message) => message.role)).toEqual(["assistant"])
  })

  test("steering is stamped but still shows the operator's own words", () => {
    const state = messagesToState([
      { role: "user", content: "[steering] 先看测试", synthetic: true },
    ])
    expect(state.messages[0]).toEqual({
      role: "user",
      parts: [{ kind: "text", text: "先看测试", streaming: false }],
    })
  })

  test("a replayed present_plan call keeps the plan it submitted", () => {
    // M3 T11: the plan's durable home is the tool call's arguments.  A
    // reopened session must be able to show the plan that was submitted,
    // even though `task.items` has long since been rewritten by the run.
    const state = messagesToState([
      {
        role: "assistant",
        content: null,
        toolCalls: [
          {
            id: "call_plan",
            name: "present_plan",
            arguments: JSON.stringify({
              plan: "# 计划\n正文",
              items: [{ content: "建目录", verify: "test -d hotel" }],
            }),
          },
        ],
      },
    ])
    const part = state.messages[0]?.parts[0]
    expect(part?.kind).toBe("tool")
    if (part?.kind !== "tool") throw new Error("not a tool part")
    expect(part.params).toEqual({
      plan: "# 计划\n正文",
      items: [{ content: "建目录", verify: "test -d hotel" }],
    })
  })

  test("an ordinary tool call does not keep its params", () => {
    // Keeping every call's arguments would turn the transcript into a copy of
    // the session file; only the plan card needs them.
    const state = messagesToState([
      {
        role: "assistant",
        content: null,
        toolCalls: [
          { id: "call_read", name: "read", arguments: '{"path":"a.py"}' },
        ],
      },
    ])
    const part = state.messages[0]?.parts[0]
    if (part?.kind !== "tool") throw new Error("not a tool part")
    expect(part.params).toBeUndefined()
  })
})
