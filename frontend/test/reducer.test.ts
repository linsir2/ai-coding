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
import { initialState, reduce } from "../src/state/session"

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

  test("message.finished with reasoning appends a reasoning part", () => {
    const state = feed(
      initialState(),
      delta("answer"),
      wire("message.finished", { text: "answer", reasoning: "because" }),
    )
    expect(lastAssistant(state).parts).toEqual([
      { kind: "text", text: "answer", streaming: false },
      { kind: "reasoning", text: "because" },
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
      tail: "",
      preview: "",
      error: "",
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
    expect(part.tail.length).toBe(4000)
    expect(part.tail.endsWith("y".repeat(100))).toBe(true)
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
      text: "✓ 子任务完成 [scout]（completed） · 会话 abcdef123456：found it · 完整回包: /tmp/x.json",
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
        "[turn] timed out: deadline",
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
