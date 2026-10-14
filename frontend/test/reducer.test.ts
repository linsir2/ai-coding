/**
 * TDD (M2): the session reducer — pure state transitions from wire events.
 *
 * Every text here is the contract the UI renders; the strings are ports of
 * ui/blocks.py (same source the retired Textual transcript used) plus
 * the approved M2 deltas (handoff, turn ERROR marker, compaction notice —
 * docs/opencode-gui-m2-plan.md §1; the approval notice moved to the M5 panel).
 */

import { describe, expect, test } from "bun:test"
import type { Action, SessionState } from "../src/state/session"
import { MAX_TRANSCRIPT_MESSAGES, initialState, reduce } from "../src/state/session"
import { toolTitle } from "../src/util/toolmeta"

const wire = (type: string, data: Record<string, unknown> = {}): Action => ({
  kind: "wire",
  event: { type, data },
})

const feed = (state: SessionState, ...actions: Action[]): SessionState =>
  actions.reduce(reduce, state)

const lastAssistant = (state: SessionState) => state.messages[state.messages.length - 1]!

const delta = (text: string) => wire("text.delta", { text })

describe("assistant text", () => {
  test("deltas accumulate into one streaming part", () => {
    const state = feed(initialState(), delta("he"), delta("llo"))
    const message = lastAssistant(state)
    expect(message.role).toBe("assistant")
    expect(message.parts).toEqual([{ kind: "text", text: "hello", streaming: true }])
  })

  test("a delta after a tool opens a new text part (multi-step turn)", () => {
    const state = feed(
      initialState(),
      delta("first"),
      wire("tool.started", { callId: "c1", toolName: "bash", params: { command: "ls" } }),
      delta("second"),
    )
    const kinds = lastAssistant(state).parts.map((part) => part.kind)
    expect(kinds).toEqual(["text", "tool", "text"])
  })

  test("message.finished replaces the streamed text with the authoritative one", () => {
    const state = feed(
      initialState(),
      delta("partial"),
      wire("message.finished", { text: "final answer", reasoning: null }),
    )
    expect(lastAssistant(state).parts).toEqual([
      { kind: "text", text: "final answer", streaming: false },
    ])
  })

  test("message.finished with null text keeps the streamed text and finalises it", () => {
    const state = feed(
      initialState(),
      delta("streamed"),
      wire("message.finished", { text: null, reasoning: null }),
    )
    expect(lastAssistant(state).parts).toEqual([
      { kind: "text", text: "streamed", streaming: false },
    ])
  })

  // A4 T1: reasoning belongs in FRONT of the text it was thinking about.  The
  // old assertion pinned the append-at-end order, which is why a finished turn
  // used to end with a dangling "+ Thought"; see docs/opencode-gui-a4-design.md.
  test("message.finished puts the step's reasoning before its text", () => {
    const state = feed(
      initialState(),
      delta("answer"),
      wire("message.finished", { text: "answer", reasoning: "because" }),
    )
    expect(lastAssistant(state).parts).toEqual([
      { kind: "reasoning", text: "because" },
      { kind: "text", text: "answer", streaming: false },
    ])
  })

  // A4 T2: a tool-only step carries text:"" — it must not leave an empty text
  // part behind (that empty part rendered as a blank line per step).
  test("a tool-only step leaves no empty text part", () => {
    const state = feed(initialState(), wire("message.finished", { text: "", reasoning: "need ls" }))
    expect(lastAssistant(state).parts).toEqual([{ kind: "reasoning", text: "need ls" }])
  })

  // A4 T3: whitespace is not a thought; it would render an empty "+ Thought".
  test("whitespace-only reasoning produces no part", () => {
    const state = feed(initialState(), wire("message.finished", { text: "hi", reasoning: "  \n " }))
    expect(lastAssistant(state).parts).toEqual([{ kind: "text", text: "hi", streaming: false }])
  })

  // M6: the event path, not just the pure function. A dispatcher emits the
  // vessel name; the card must end up naming the server and tool that ran, or
  // a whole session of MCP calls reads as "mcp_call".
  test("an mcp_call event puts the resolved name on the card", () => {
    const state = feed(
      initialState(),
      wire("tool.started", {
        callId: "c1",
        toolName: "mcp_call",
        params: { server: "fs", tool: "echo", input: { text: "hi" } },
      }),
      wire("tool.finished", {
        callId: "c1",
        toolName: "mcp_call",
        success: true,
        outputPreview: "echo:hi",
        durationMs: 5,
        error: null,
      }),
    )
    const part = lastAssistant(state).parts.find((entry) => entry.kind === "tool")
    if (part?.kind !== "tool") throw new Error("no tool part")
    expect(part).toMatchObject({ toolName: "mcp_call", target: "fs__echo" })
    expect(toolTitle(part.verb, part.target, false)).toBe("已运行工具 · fs__echo")
  })

  // A4 T4: the two-step turn from the bug report — each step's reasoning sits
  // in front of that step's own output, tool card included.
  test("a two-step turn keeps each reasoning in front of its own output", () => {
    const state = feed(
      initialState(),
      wire("message.finished", { text: "", reasoning: "need the branch list" }),
      wire("tool.started", { callId: "c1", toolName: "bash", params: { command: "git branch" } }),
      wire("tool.finished", {
        callId: "c1",
        toolName: "bash",
        success: true,
        outputPreview: "* main",
        durationMs: 5,
        error: null,
      }),
      delta("You are on main"),
      wire("message.finished", { text: "You are on main", reasoning: "summarise it" }),
    )
    expect(lastAssistant(state).parts.map((part) => part.kind)).toEqual([
      "reasoning",
      "tool",
      "reasoning",
      "text",
    ])
  })

  test("authoritative text lands even when no delta was streamed", () => {
    const state = feed(initialState(), wire("message.finished", { text: "direct", reasoning: null }))
    expect(lastAssistant(state).parts).toEqual([{ kind: "text", text: "direct", streaming: false }])
  })

  test("unknown wire types are ignored (forward compatible)", () => {
    const state = feed(initialState(), wire("some.future.event", { x: 1 }))
    expect(state).toEqual(initialState())
  })
})

describe("tools", () => {
  test("tool.started creates a running card with derived verb and target", () => {
    const state = feed(
      initialState(),
      wire("tool.started", {
        callId: "c1",
        toolName: "bash",
        params: { command: "ls -la" },
        iteration: 1,
      }),
    )
    expect(lastAssistant(state).parts[0]).toEqual({
      kind: "tool",
      callId: "c1",
      toolName: "bash",
      verb: "运行命令",
      target: "ls -la",
      status: "running",
      lines: [],
      pending: "",
      linesDropped: false,
      preview: "",
      error: "",
      durationMs: 0,
    })
  })

  test("tool.finished updates the matching card", () => {
    const state = feed(
      initialState(),
      wire("tool.started", { callId: "c1", toolName: "read", params: { path: "/a" } }),
      wire("tool.finished", {
        callId: "c1",
        toolName: "read",
        success: true,
        outputPreview: "line one",
        durationMs: 5,
        error: null,
      }),
    )
    const part = lastAssistant(state).parts[0]
    expect(part).toMatchObject({ kind: "tool", status: "ok", preview: "line one" })
  })

  test("tool.finished for an unseen call still lands as a settled card", () => {
    const state = feed(
      initialState(),
      wire("tool.finished", {
        callId: "lost",
        toolName: "glob",
        success: false,
        outputPreview: "",
        error: "tool_timeout",
      }),
    )
    expect(lastAssistant(state).parts[0]).toMatchObject({
      kind: "tool",
      callId: "lost",
      status: "failed",
      error: "tool_timeout",
    })
  })

  test("tool.chunk appends to the card tail, capped to the last 4000 chars", () => {
    const big = "x".repeat(3000)
    const state = feed(
      initialState(),
      wire("tool.started", { callId: "c1", toolName: "bash", params: { command: "yes" } }),
      wire("tool.chunk", { callId: "c1", stream: "stdout", text: big }),
      wire("tool.chunk", { callId: "c1", stream: "stdout", text: "y".repeat(1500) }),
    )
    const part = lastAssistant(state).parts[0]
    if (part.kind !== "tool") throw new Error("expected tool part")
    // Same budget as before, counted across the lines it is kept in: an open
    // line has no newline after it, so it costs its length and nothing more.
    const kept = part.lines.reduce((sum, line) => sum + line.text.length + 1, -1)
    expect(kept).toBeLessThanOrEqual(4000)
    expect(kept).toBeGreaterThan(3900) // the cap was actually reached
    expect(part.lines.at(-1)!.text.endsWith("y".repeat(100))).toBe(true)
    expect(part.linesDropped).toBe(true)
  })


  // A5 T3/T4: duration and truncation are facts the wire already sent and the
  // card now shows; a dropped tail head must be recorded, not silently lost.
  test("tool.finished records the duration the wire measured", () => {
    const state = feed(
      initialState(),
      wire("tool.started", { callId: "c1", toolName: "bash", params: { command: "ls" } }),
      wire("tool.finished", {
        callId: "c1",
        toolName: "bash",
        success: true,
        outputPreview: "line one",
        durationMs: 820,
        error: null,
      }),
    )
    const part = lastAssistant(state).parts[0]
    if (part.kind !== "tool") throw new Error("expected tool part")
    expect(part.durationMs).toBe(820)
    expect(part.linesDropped).toBe(false)
  })

  test("tool.chunk carries each line's stream and joins a line split across chunks", () => {
    const state = feed(
      initialState(),
      wire("tool.started", { callId: "c1", toolName: "bash", params: { command: "cc" } }),
      wire("tool.chunk", { callId: "c1", text: "warn: ", stream: "stderr" }),
      wire("tool.chunk", { callId: "c1", text: "unused\n", stream: "stderr" }),
      wire("tool.chunk", { callId: "c1", text: "ok\n", stream: "stdout" }),
    )
    const part = lastAssistant(state).parts[0]
    if (part.kind !== "tool") throw new Error("expected tool part")
    expect(part.lines).toEqual([
      { text: "warn: unused", stream: "stderr" },
      { text: "ok", stream: "stdout" },
    ])
    expect(part.pending).toBe("")
  })

  test("a tool call the card cannot expand past records that the head was dropped", () => {
    const state = feed(
      initialState(),
      wire("tool.started", { callId: "c1", toolName: "bash", params: { command: "yes" } }),
      wire("tool.chunk", { callId: "c1", text: "x".repeat(4000) }),
      wire("tool.chunk", { callId: "c1", text: "y\n" }),
    )
    const part = lastAssistant(state).parts[0]
    if (part.kind !== "tool") throw new Error("expected tool part")
    const kept = part.lines.reduce((sum, line) => sum + line.text.length + 1, 0)
    expect(kept).toBeLessThanOrEqual(4000)
    expect(part.linesDropped).toBe(true)
  })

  test("a short tool call records no truncation", () => {
    const state = feed(
      initialState(),
      wire("tool.started", { callId: "c1", toolName: "bash", params: { command: "ls" } }),
      wire("tool.chunk", { callId: "c1", text: "short" }),
    )
    const part = lastAssistant(state).parts[0]
    if (part.kind !== "tool") throw new Error("expected tool part")
    expect(part.linesDropped).toBe(false)
  })

  test("output preview is shortened to 120 chars like the old fold body", () => {
    const state = feed(
      initialState(),
      wire("tool.finished", {
        callId: "c1",
        toolName: "bash",
        success: true,
        outputPreview: "z".repeat(200),
        error: null,
      }),
    )
    const part = lastAssistant(state).parts[0]
    if (part.kind !== "tool") throw new Error("expected tool part")
    expect(part.preview.length).toBe(121)
    expect(part.preview.endsWith("…")).toBe(true)
  })
})

describe("plan and ledger", () => {
  test("plan.updated appends once and replaces in place on refresh", () => {
    const first = wire("plan.updated", { display: "- one", done: 0, total: 1 })
    const second = wire("plan.updated", { display: "✓ one", done: 1, total: 1 })
    const state = feed(initialState(), delta("x"), first, second)
    const plans = lastAssistant(state).parts.filter((part) => part.kind === "plan")
    expect(plans).toEqual([{ kind: "plan", title: "计划 1/1", body: "✓ one" }])
  })

  test("ledger.updated renders level, question and evidence lines", () => {
    const state = feed(
      initialState(),
      wire("ledger.updated", {
        items: [{ level: "high", question: "is it?", evidence: ["log line", "diff"] }],
      }),
    )
    expect(lastAssistant(state).parts[0]).toEqual({
      kind: "notice",
      tone: "muted",
      text: "[high] is it?\n  · log line\n  · diff",
    })
  })
})

describe("notices", () => {
  test("provider retries, fallbacks and tool-surface warnings", () => {
    const state = feed(
      initialState(),
      wire("provider.retried", { kind: "timeout", attempt: 2, delaySeconds: 1.5, reason: "slow" }),
      wire("provider.fallback", { fromModel: "a", toModel: "b", kind: "error", reason: "" }),
      wire("tools.warning", { toolsChars: 900, threshold: 800, origins: [["mcp", 500]] }),
    )
    expect(lastAssistant(state).parts.map((part) => (part as { text: string }).text)).toEqual([
      "⤷ 重试 [timeout] 第 2 次，等 1.5s：slow",
      "⤬ 降级 a → b（error）",
      "[tools] 900 字符（阈值 800）  mcp=500",
    ])
  })

  test("approval.requested stays off the transcript (M5: the panel owns it)", () => {
    const state = feed(
      initialState(),
      wire("approval.requested", { toolName: "bash", params: {}, reason: "warn tier" }),
    )
    expect(state).toEqual(initialState())
  })

  test("context.compacted makes the history rewrite visible", () => {
    const state = feed(initialState(), wire("context.compacted", { beforeMessages: 12, afterMessages: 4 }))
    expect(lastAssistant(state).parts[0]).toEqual({
      kind: "notice",
      tone: "muted",
      text: "[context] 上下文已压缩 12→4 条消息",
    })
  })

  test("background.done lands on the last assistant message even across turns", () => {
    const state = feed(
      initialState(),
      { kind: "submitted", text: "hi" },
      delta("done"),
      wire("turn.finished", { status: "completed", reason: "", iterations: 1, truncatedByLength: false, handoff: "" }),
      wire("background.done", {
        runId: "r1",
        name: "scout",
        status: "completed",
        summary: "found it",
        truncated: false,
        originSession: "abcdef1234567890",
        ledgerPath: "/tmp/x.json",
      }),
    )
    const messages = state.messages.filter((m) => m.role === "assistant")
    expect(messages).toHaveLength(1)
    expect(messages[0]!.parts.at(-1)).toEqual({
      kind: "notice",
      tone: "muted",
      text: "✓ 子任务完成 [scout]（completed） · 会话 abcdef123456：found it · 完整回包: /tmp/x.json · 任务 r1",
    })
  })

  test("subagent events render one dim timeline line each", () => {
    const state = feed(
      initialState(),
      wire("subagent.event", {
        scope: "scout",
        inner: { kind: "tool", phase: "started", toolName: "read", params: { path: "/a" } },
      }),
      wire("subagent.event", {
        scope: "scout",
        inner: { kind: "tool", phase: "finished", toolName: "read", success: true, outputPreview: "ok", error: null },
      }),
      wire("subagent.event", { scope: "scout", inner: { kind: "text", text: "child says hi" } }),
    )
    expect(lastAssistant(state).parts.map((part) => (part as { text: string }).text)).toEqual([
      "↳ [scout] ▶ 读取文件 /a",
      "↳ [scout] ✓ read ok",
      "↳ [scout] child says hi",
    ])
  })
})

  test("background.done says when the summary was cut", () => {
    const state = feed(
      initialState(),
      { kind: "submitted", text: "hi" },
      delta("done"),
      wire("background.done", {
        runId: "abcdef0123456789",
        name: "scout",
        status: "completed",
        summary: "and then it kept going",
        truncated: true,
        originSession: "",
        ledgerPath: "",
      }),
    )
    const notice = lastAssistant(state).parts.at(-1)
    expect(notice).toMatchObject({ kind: "notice" })
    expect((notice as { text: string }).text).toContain("截断")
    // 12 chars is what /tasks prints, so the id on screen can be pasted there.
    expect((notice as { text: string }).text).toContain("任务 abcdef012345")
  })

describe("turn lifecycle", () => {
  test("completed turns are silent and free the prompt", () => {
    const state = feed(
      initialState(),
      { kind: "submitted", text: "hi" },
      delta("answer"),
      wire("turn.finished", { status: "completed", reason: "", iterations: 1, truncatedByLength: false, handoff: "" }),
    )
    expect(state.busy).toBe(false)
    expect(lastAssistant(state).parts).toEqual([
      { kind: "text", text: "answer", streaming: false },
    ])
  })

  test("aborted, timeout, truncation, budget cut and engine errors are red markers", () => {
    const cases: Array<[Record<string, unknown>, string]> = [
      [
        { status: "aborted", reason: "", iterations: 2, truncatedByLength: false, handoff: "" },
        "[turn] 已中断（Esc），任务未收尾",
      ],
      [
        { status: "timeout", reason: "deadline", iterations: 2, truncatedByLength: false, handoff: "" },
        "[turn] 超时：deadline",
      ],
      // The two statuses the loop can end on that had no words at all: the
      // turn stopped and the transcript said nothing, so it read as silence.
      [
        { status: "budget", reason: "token ceiling", iterations: 3, truncatedByLength: false, handoff: "" },
        "[turn] 预算用完（token ceiling），输入「继续」可接上",
      ],
      [
        { status: "stopped", reason: "", iterations: 3, truncatedByLength: false, handoff: "" },
        "[turn] 已停止（operator asked to stop）",
      ],
      [
        { status: "completed", reason: "", iterations: 2, truncatedByLength: true, handoff: "" },
        "[turn] 回答因长度上限被截断：输入「继续」可接上",
      ],
      [
        { status: "max_iterations", reason: "", iterations: 10, truncatedByLength: false, handoff: "" },
        "[turn] 步数用完（10 步），任务未收尾：输入「继续」可接上",
      ],
      [
        { status: "error", reason: "RuntimeError: boom", iterations: 0, truncatedByLength: false, handoff: "" },
        "[turn] 错误：RuntimeError: boom",
      ],
    ]
    for (const [data, text] of cases) {
      const state = feed(initialState(), { kind: "submitted", text: "q" }, wire("turn.finished", data))
      const tail = lastAssistant(state).parts.at(-1)
      expect(tail).toEqual({ kind: "turn-end", tone: "error", text })
      expect(state.busy).toBe(false)
    }
  })

  test("handoff is appended as assistant prose when the model never spoke", () => {
    const state = feed(
      initialState(),
      { kind: "submitted", text: "q" },
      wire("turn.finished", {
        status: "max_iterations",
        reason: "",
        iterations: 10,
        truncatedByLength: false,
        handoff: "目标未完成：还差测试",
      }),
    )
    const parts = lastAssistant(state).parts
    expect(parts).toContainEqual({ kind: "text", text: "目标未完成：还差测试", streaming: false })
  })

  test("submit marks busy and a failed submit reports over IPC", () => {
    const busy = feed(initialState(), { kind: "submitted", text: "hi" })
    expect(busy.busy).toBe(true)
    expect(busy.messages[0]).toEqual({ role: "user", parts: [{ kind: "text", text: "hi", streaming: false }] })
    const failed = feed(busy, { kind: "submit-failed", reason: "ipc client not connected" })
    expect(failed.busy).toBe(false)
    expect(lastAssistant(failed).parts[0]).toEqual({
      kind: "notice",
      tone: "error",
      text: "[ipc] ipc client not connected",
    })
  })

  test("a notice arriving with no messages yet opens an assistant message", () => {
    const state = feed(initialState(), wire("provider.retried", { kind: "x", attempt: 1, delaySeconds: 2, reason: "" }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0]!.role).toBe("assistant")
  })
})

describe("input queue", () => {
  const finished = wire("turn.finished", {
    status: "completed",
    reason: "",
    iterations: 1,
    truncatedByLength: false,
    handoff: "",
  })

  test("submitting while busy queues the line and shows it as a user message", () => {
    const running = feed(initialState(), { kind: "submitted", text: "first" })
    const queued = feed(running, { kind: "queued", text: "second" })
    expect(queued.busy).toBe(true)
    expect(queued.queued).toEqual(["second"])
    expect(queued.messages.map((message) => message.role)).toEqual(["user", "user"])
  })

  test("turn.finished keeps the queue (lines are never dropped)", () => {
    const queued = feed(
      initialState(),
      { kind: "submitted", text: "a" },
      { kind: "queued", text: "b" },
      finished,
    )
    expect(queued.busy).toBe(false)
    expect(queued.queued).toEqual(["b"])
  })

  test("queued-dispatched pops the first line, stays busy, and does not re-append it", () => {
    const queued = feed(
      initialState(),
      { kind: "submitted", text: "a" },
      { kind: "queued", text: "b" },
      { kind: "queued", text: "c" },
      finished,
    )
    const dispatched = feed(queued, { kind: "queued-dispatched" })
    expect(dispatched.busy).toBe(true)
    expect(dispatched.queued).toEqual(["c"])
    expect(dispatched.messages.filter((message) => message.role === "user")).toHaveLength(3)
  })

  test("a refused paste says so on the message that showed it", () => {
    // r20/F5: the picture is drawn the moment the user hits send, so the
    // transcript already claims it went out.  The refusal has to land on that
    // same message, or the user reads a sent image the model never received.
    const busy = feed(initialState(), { kind: "submitted", text: "看这个", images: [{ name: "shot.png", dataUrl: "data:image/png;base64,AA" }] })
    const after = feed(busy, {
      kind: "attachment-rejected",
      items: [{ name: "shot.png", reason: "不支持的图片格式：text/plain" }],
    })

    expect(after.busy).toBe(true), "a refused attachment is not a failed submit"
    const parts = after.messages[0]!.parts
    expect(parts.some((part) => part.kind === "notice")).toBe(true)
    expect(JSON.stringify(parts)).toContain("text/plain")
  })

  test("an accepted paste adds no notice", () => {
    const busy = feed(initialState(), { kind: "submitted", text: "hi" })
    const after = feed(busy, { kind: "attachment-rejected", items: [] })

    expect(after.messages[0]!.parts).toHaveLength(1)
  })

  test("submit-failed keeps whatever is still queued", () => {
    const queued = feed(initialState(), { kind: "queued", text: "b" })
    const failed = feed(queued, { kind: "submit-failed", reason: "boom" })
    expect(failed.queued).toEqual(["b"])
    expect(failed.busy).toBe(false)
  })
})

describe("slash commands over IPC (session.command)", () => {
  const done = {
    status: "completed",
    reason: "",
    iterations: 1,
    truncatedByLength: false,
    handoff: "",
  }

  test("console.line lands as a notice: the command's own output", () => {
    const state = feed(
      initialState(),
      { kind: "submitted", text: "/help" },
      wire("console.line", { tone: "info", text: "[status] session=abc messages=3" }),
      wire("turn.finished", done),
    )
    const parts = lastAssistant(state).parts
    expect(parts).toContainEqual({ kind: "notice", tone: "muted", text: "[status] session=abc messages=3" })
    expect(state.busy).toBe(false)
  })

  test("an error line keeps the error tone", () => {
    const state = feed(
      initialState(),
      wire("console.line", { tone: "error", text: "unknown command '/plan-run'" }),
    )
    expect(lastAssistant(state).parts).toContainEqual({
      kind: "notice",
      tone: "error",
      text: "unknown command '/plan-run'",
    })
  })

  test("a command's inner turn is a step: streaming settles, the run stays busy", () => {
    const state = feed(
      initialState(),
      { kind: "submitted", text: "/review" },
      wire("text.delta", { text: "reviewing" }),
      wire("turn.step", { ...done, text: "reviewing" }),
    )
    // the step settled the streaming text ...
    expect(lastAssistant(state).parts).toContainEqual({ kind: "text", text: "reviewing", streaming: false })
    // ... but the command is still running: no second message, no free prompt
    expect(state.busy).toBe(true)
    expect(state.messages.filter((m) => m.role === "assistant")).toHaveLength(1)
  })

  test("the run ends once, on the terminal turn.finished", () => {
    const state = feed(
      initialState(),
      { kind: "submitted", text: "/review" },
      wire("turn.step", done),
      wire("turn.finished", done),
    )
    expect(state.busy).toBe(false)
    expect(state.messages.filter((m) => m.role === "assistant")).toHaveLength(1)
  })
})

describe("compaction lifecycle (P3-2)", () => {
  test("started opens a running compaction row, busy stays true", () => {
    const state = feed(initialState(), wire("compaction.started", { reason: "manual" }))
    expect(lastAssistant(state).parts).toEqual([
      {
        kind: "compaction",
        status: "running",
        reason: "manual",
        text: "",
        beforeMessages: 0,
        afterMessages: 0,
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        error: "",
      },
    ])
    expect(state.busy).toBe(false)
  })

  test("deltas accumulate into the running row", () => {
    const state = feed(
      initialState(),
      wire("compaction.started", { reason: "auto" }),
      wire("compaction.delta", { text: "## 目标\n" }),
      wire("compaction.delta", { text: "done" }),
    )
    expect(lastAssistant(state).parts[0]).toMatchObject({
      kind: "compaction",
      status: "running",
      text: "## 目标\ndone",
    })
  })

  test("ended finalises the row with counts, usage and the authoritative text", () => {
    const state = feed(
      initialState(),
      wire("compaction.started", { reason: "auto" }),
      wire("compaction.delta", { text: "partial" }),
      wire("compaction.ended", {
        reason: "auto",
        beforeMessages: 74,
        afterMessages: 3,
        inputTokens: 1312,
        outputTokens: 147,
        totalTokens: 1896,
        text: "## 目标\nfull",
      }),
    )
    expect(lastAssistant(state).parts[0]).toEqual({
      kind: "compaction",
      status: "done",
      reason: "auto",
      text: "## 目标\nfull",
      beforeMessages: 74,
      afterMessages: 3,
      inputTokens: 1312,
      outputTokens: 147,
      totalTokens: 1896,
      error: "",
    })
  })

  test("ended without a started row still lands a done row", () => {
    const state = feed(
      initialState(),
      wire("compaction.ended", {
        reason: "overflow",
        beforeMessages: 10,
        afterMessages: 2,
        inputTokens: 5,
        outputTokens: 1,
        totalTokens: 6,
        text: "## 目标\nx",
      }),
    )
    expect(lastAssistant(state).parts[0]).toMatchObject({ kind: "compaction", status: "done" })
  })

  test("ended swallows the legacy context.compacted one-liner", () => {
    const state = feed(
      initialState(),
      wire("compaction.started", { reason: "auto" }),
      wire("context.compacted", { beforeMessages: 74, afterMessages: 3 }),
      wire("compaction.ended", {
        reason: "auto",
        beforeMessages: 74,
        afterMessages: 3,
        inputTokens: 1,
        outputTokens: 1,
        totalTokens: 2,
        text: "## 目标\nfull",
      }),
    )
    const texts = lastAssistant(state).parts.map((p) => p.kind)
    expect(texts).toEqual(["compaction"])
  })

  test("failed turns the row red exactly once (no duplicate notice)", () => {
    // The card renders ✗ 压缩失败：{error} itself; a same-text notice beside it
    // doubled the report (measured live: two identical ✗ lines per failure).
    const state = feed(
      initialState(),
      wire("compaction.started", { reason: "manual" }),
      wire("compaction.failed", { reason: "manual", error: "boom" }),
    )
    expect(lastAssistant(state).parts).toEqual([
      {
        kind: "compaction",
        status: "failed",
        reason: "manual",
        text: "",
        beforeMessages: 0,
        afterMessages: 0,
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        error: "boom",
      },
    ])
  })

  test("a failure with no running row still lands one red card", () => {
    const state = feed(
      initialState(),
      wire("compaction.failed", { reason: "auto", error: "late" }),
    )
    expect(lastAssistant(state).parts).toEqual([
      {
        kind: "compaction",
        status: "failed",
        reason: "auto",
        text: "",
        beforeMessages: 0,
        afterMessages: 0,
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        error: "late",
      },
    ])
  })

  test("a skip replaces the running row with a muted card, not an error", () => {
    // Decided 2026-10-14: "nothing worth summarising" is a skip, rendered
    // muted; only a failed summary call turns red.
    const state = feed(
      initialState(),
      wire("compaction.started", { reason: "manual" }),
      wire("compaction.skipped", { reason: "manual" }),
    )
    expect(lastAssistant(state).parts).toEqual([
      {
        kind: "compaction",
        status: "skipped",
        reason: "manual",
        text: "",
        beforeMessages: 0,
        afterMessages: 0,
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        error: "",
      },
    ])
  })

  test("a skip with no running row still lands one muted card", () => {
    const state = feed(initialState(), wire("compaction.skipped", { reason: "auto" }))
    expect(lastAssistant(state).parts).toEqual([
      {
        kind: "compaction",
        status: "skipped",
        reason: "auto",
        text: "",
        beforeMessages: 0,
        afterMessages: 0,
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        error: "",
      },
    ])
  })
})

describe("transcript retirement (P3-4)", () => {
  const submitMany = (n: number): SessionState => {
    let state = initialState()
    for (let i = 0; i < n; i++) state = reduce(state, { kind: "submitted", text: `m${i}` })
    return state
  }

  const doneCard = (text = "## 目标\nx") => [
    wire("compaction.started", { reason: "manual" }),
    wire("compaction.ended", {
      reason: "manual",
      beforeMessages: 29,
      afterMessages: 2,
      inputTokens: 1,
      outputTokens: 1,
      totalTokens: 2,
      text,
    }),
  ]

  test("past the cap the oldest messages retire silently", () => {
    const state = submitMany(MAX_TRANSCRIPT_MESSAGES + 1)
    expect(state.messages).toHaveLength(MAX_TRANSCRIPT_MESSAGES)
    expect(state.messages[0]).toEqual({
      role: "user",
      parts: [{ kind: "text", text: "m1", streaming: false }],
    })
    expect(state.messages[MAX_TRANSCRIPT_MESSAGES - 1]).toEqual({
      role: "user",
      parts: [{ kind: "text", text: `m${MAX_TRANSCRIPT_MESSAGES}`, streaming: false }],
    })
    const notices = state.messages.flatMap((m) => m.parts).filter((p) => p.kind === "notice")
    expect(notices).toHaveLength(0)
  })

  test("a running compaction row at the tail survives retirement", () => {
    let state = submitMany(MAX_TRANSCRIPT_MESSAGES - 1)
    state = reduce(state, wire("compaction.started", { reason: "manual" }))
    expect(state.messages).toHaveLength(MAX_TRANSCRIPT_MESSAGES)
    // One more row forces an eviction; the running card is the tail, not the victim.
    state = reduce(state, { kind: "submitted", text: "new" })
    state = reduce(state, wire("compaction.delta", { text: "## 目标\n" }))
    expect(state.messages).toHaveLength(MAX_TRANSCRIPT_MESSAGES)
    const card = state.messages
      .flatMap((m) => m.parts)
      .find((p) => p.kind === "compaction")
    expect(card).toEqual({
      kind: "compaction",
      status: "running",
      reason: "manual",
      text: "## 目标\n",
      beforeMessages: 0,
      afterMessages: 0,
      inputTokens: 0,
      outputTokens: 0,
      totalTokens: 0,
      error: "",
    })
  })

  test("a finished compaction card is exempt from retirement", () => {
    let state = feed(initialState(), ...doneCard())
    for (let i = 0; i < MAX_TRANSCRIPT_MESSAGES; i++) {
      state = reduce(state, { kind: "submitted", text: `m${i}` })
    }
    // 101 rows, one exempt: the oldest plain row retires instead of the card.
    // The card was first, so it still heads the transcript after m0 retires.
    expect(state.messages).toHaveLength(MAX_TRANSCRIPT_MESSAGES)
    expect(
      state.messages.some((m) => m.parts.some((part) => part.kind === "compaction")),
    ).toBe(true)
    expect(state.messages[1]).toEqual({
      role: "user",
      parts: [{ kind: "text", text: "m1", streaming: false }],
    })
  })

  test("eviction on the event path leaves one muted notice, not one per eviction", () => {
    let state = submitMany(MAX_TRANSCRIPT_MESSAGES - 1)
    state = reduce(state, wire("compaction.started", { reason: "manual" }))
    // At cap; one more event row evicts the oldest and explains itself once.
    state = reduce(state, { kind: "submitted", text: "new" })
    state = reduce(state, wire("compaction.started", { reason: "auto" }))
    state = reduce(state, { kind: "submitted", text: "newer" })
    state = reduce(state, wire("compaction.started", { reason: "auto" }))
    const notices = state.messages
      .flatMap((m) => m.parts)
      .filter((p) => p.kind === "notice" && p.tone === "muted")
    expect(notices).toHaveLength(1)
    expect(state.messages).toHaveLength(MAX_TRANSCRIPT_MESSAGES)
  })
})

describe("compaction cards survive interleaved input (P3-4)", () => {
  test("a delta finds the running card even after a queued user row", () => {
    let state = feed(
      initialState(),
      { kind: "submitted", text: "/compact" },
      wire("compaction.started", { reason: "manual" }),
      { kind: "queued", text: "别停" },
      wire("compaction.delta", { text: "## 目标\n" }),
    )
    const cards = state.messages.flatMap((m) => m.parts).filter((p) => p.kind === "compaction")
    expect(cards).toHaveLength(1)
    expect(cards[0]).toMatchObject({ status: "running", text: "## 目标\n" })
    // No empty assistant message was created for the homeless chunk.
    expect(state.messages.every((m) => m.role === "user" || m.parts.length > 0)).toBe(true)
  })

  test("ended finalises the running card in place, no duplicate row", () => {
    let state = feed(
      initialState(),
      { kind: "submitted", text: "/compact" },
      wire("compaction.started", { reason: "manual" }),
      { kind: "queued", text: "别停" },
      wire("compaction.ended", {
        reason: "manual",
        beforeMessages: 29,
        afterMessages: 2,
        inputTokens: 1,
        outputTokens: 1,
        totalTokens: 2,
        text: "## 目标\nx",
      }),
    )
    const cards = state.messages.flatMap((m) => m.parts).filter((p) => p.kind === "compaction")
    expect(cards).toHaveLength(1)
    expect(cards[0]).toMatchObject({ status: "done", text: "## 目标\nx" })
  })
})


describe("image attachments (r17/T5)", () => {
  test("a submitted image lands as its own part", () => {
    const state = reduce(initialState(), {
      kind: "submitted",
      text: "看这个",
      images: [{ name: "shot", dataUrl: "data:image/png;base64,AA" }],
    })

    expect(state.messages[0]!.parts).toEqual([
      { kind: "text", text: "看这个", streaming: false },
      { kind: "image", name: "shot" },
    ])
  })

  test("an image-only submission has no empty text part", () => {
    const state = reduce(initialState(), {
      kind: "submitted",
      text: "",
      images: [{ name: "shot", dataUrl: "data:image/png;base64,AA" }],
    })

    expect(state.messages[0]!.parts).toEqual([{ kind: "image", name: "shot" }])
  })
})
