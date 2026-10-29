/**
 * TDD (M2): tool card metadata — a faithful port of the verb table and target
 * extraction the retired Textual transcript used (ui/blocks.py), minus
 * the `kind` labels: those only ever grouped folds, which the parts model
 * does not have.  What the card renders is exactly what is pinned here.
 */

import { describe, expect, test } from "bun:test"
import {
  foldToolChunk,
  formatDuration,
  formatToolDuration,
  toolFailure,
  toolTarget,
  toolTitle,
  toolVerb,
  truncationNote,
} from "../src/util/toolmeta"
import type { TailLine } from "../src/util/toolmeta"

let dropped = false
const TAIL_CHAR_LIMIT = 4000

describe("toolVerb", () => {
  test("known tools use the ported Chinese verbs", () => {
    expect(toolVerb("bash")).toBe("运行命令")
    expect(toolVerb("read")).toBe("读取文件")
    expect(toolVerb("edit")).toBe("编辑文件")
    expect(toolVerb("todo_write")).toBe("更新计划")
    // 20 §批次2: grep had no verb, so every search drew as 运行工具.
    expect(toolVerb("grep")).toBe("搜索内容")
    // C-1: without a verb the recall call draws as 运行工具, which says
    // nothing about what was searched.
    expect(toolVerb("history_search")).toBe("回忆历史")
  })

  test("unknown tools (MCP included) fall back to 运行工具", () => {
    expect(toolVerb("mcp__search__web")).toBe("运行工具")
  })
})

describe("toolTarget", () => {
  test("glob reads pattern, file tools read path", () => {
    expect(toolTarget("glob", { pattern: "src/**/*.ts" })).toBe("src/**/*.ts")
    expect(toolTarget("read", { path: "/tmp/a.txt" })).toBe("/tmp/a.txt")
    expect(toolTarget("edit", { path: "/tmp/a.txt" })).toBe("/tmp/a.txt")
  })

  test("bash flattens whitespace and truncates at 60 chars with an ellipsis", () => {
    expect(toolTarget("bash", { command: "ls -la\n  /tmp" })).toBe("ls -la /tmp")
    const long = "echo " + "x".repeat(80)
    const target = toolTarget("bash", { command: long })
    expect(target).toHaveLength(61)
    expect(target.endsWith("…")).toBe(true)
  })

  test("an MCP call is shown by what it actually calls, not by the vessel", () => {
    // The dispatcher emits tool.started with the vessel name; the resolved pair
    // is in params. Without this the transcript reads "运行工具 · mcp_call" and
    // the operator cannot tell which server ran, or which of its tools.
    expect(
      toolTarget("mcp_call", { server: "fs", tool: "echo", input: { text: "hi" } }),
    ).toBe("fs__echo")
  })

  test("a half-formed MCP call does not render a broken name", () => {
    expect(toolTarget("mcp_call", { server: "fs" })).toBe("mcp_call")
    expect(toolTarget("mcp_call", { tool: "echo" })).toBe("mcp_call")
  })

  test("the already-resolved name still resolves to itself", () => {
    // The permission panel receives fs__echo, not the vessel: it must not go
    // through this branch.
    expect(toolTarget("fs__echo", { server: "fs", tool: "echo", input: {} })).toBe("fs__echo")
  })

  test("unknown tools identify themselves by name", () => {
    expect(toolTarget("mcp__search__web", { query: "x" })).toBe("mcp__search__web")
  })

  test("known targetless tools stay bare", () => {
    expect(toolTarget("todo_write", { tasks: [] })).toBe("")
    expect(toolTarget("skill", { name: "x" })).toBe("")
  })

  test("missing params never throw", () => {
    expect(toolTarget("read", {})).toBe("")
    expect(toolTarget("bash", {})).toBe("")
  })
})

describe("toolTitle", () => {
  test("running uses 正在, settled uses 已", () => {
    expect(toolTitle("运行命令", "ls", true)).toBe("正在运行命令 · ls")
    expect(toolTitle("运行命令", "ls", false)).toBe("已运行命令 · ls")
  })

  test("no target renders the bare action", () => {
    expect(toolTitle("更新计划", "", false)).toBe("已更新计划")
  })

  // R22 A-17: a failed call must not claim it ran -- the subtitle already says
  // why, the title only has to stop lying.
  test("a failed call does not claim it ran", () => {
    expect(toolTitle("运行命令", "ls", false, false)).toBe("已尝试运行命令 · ls")
  })

  test("a denied call also does not claim it ran", () => {
    expect(toolTitle("编辑文件", "a.py", false, false)).toBe("已尝试编辑文件 · a.py")
  })
})

/**
 * A5 T1/T2: the card must never show a machine code.  The table is transcribed
 * from docs/data-contracts.md:159,167,347 — the documented `ToolResult.error`
 * contract — not invented here; a code the backend adds later shows up as
 * `未知原因（code）` rather than disappearing.
 */
describe("toolFailure", () => {
  test("every documented code maps to a label and a tone", () => {
    const denied: Array<[string, string]> = [
      ["denied", "被拒绝"],
      ["not_approved", "未获批准"],
      ["guard_denied", "门卫拦截"],
      ["hook_blocked", "钩子拦截"],
      ["approval_timeout", "审批超时"],
      ["stopped", "已停止"],
      ["skipped", "已跳过"],
      // 20 §批次2: present_plan's verdicts are refusals, not breakage --
      // they must not surface as 未知原因 in the middle of the planning flow.
      ["no_plan_items", "计划尚未写成条目"],
      ["missing_plan", "计划内容为空"],
      ["no_user", "无人评审"],
      // 内化批次: the readonly gate's own code was the last live 未知原因
      // (found in session 7ad86804's transcript).
      ["readonly_denied", "只读拒绝"],
      // R03: present_plan's posture gates -- a step or plain session calling
      // it gets a verdict, not 未知原因.
      ["not_in_plan", "非计划模式"],
      ["task_busy", "任务运行中"],
      // R08: keep planning is the user's revision request, not breakage.
      ["plan_keep", "评审要求继续规划"],
      // R21 §3: a plan-step turn has no running goal, so update_goal there is
      // misplaced rather than an unknown cause.
      ["no_goal", "无运行中的目标"],
    ]
    for (const [code, label] of denied) {
      expect(toolFailure(code)).toEqual({ label, tone: "denied" })
    }
    const failed: Array<[string, string]> = [
      ["nonzero_exit", "命令返回非零"],
      ["timeout", "命令超时"],
      ["spawn_failed", "命令无法启动"],
      ["tool_timeout", "工具调用超时"],
      ["tool_error", "工具异常"],
      ["unknown_tool", "未知工具"],
      ["mcp_error", "MCP 调用失败"],
      ["subagent_error", "子代理失败"],
      ["user_prompt_error", "评审通道异常"],
    ]
    for (const [code, label] of failed) {
      expect(toolFailure(code)).toEqual({ label, tone: "failed" })
    }
  })

  test("a code the table does not know still surfaces it", () => {
    expect(toolFailure("brand_new_code")).toEqual({
      label: "未知原因（brand_new_code）",
      tone: "failed",
    })
  })

  test("an empty error has no label to show", () => {
    expect(toolFailure("").label).toBe("")
  })
})

describe("formatDuration", () => {
  test("keeps the unit the reader expects at each scale", () => {
    expect(formatDuration(0)).toBe("0ms")
    expect(formatDuration(820)).toBe("820ms")
    expect(formatDuration(1500)).toBe("1.5s")
    expect(formatDuration(125000)).toBe("2m 5s")
  })
})

describe("toolFailure", () => {
  test("a sandbox that cannot run is an environment failure, not a refusal (A-4)", () => {
    expect(toolFailure("sandbox_unavailable")).toEqual({
      label: "沙箱不可用（命令未执行）",
      tone: "failed",
    })
    // A real refusal keeps its own words and its own tone.
    expect(toolFailure("denied")).toEqual({ label: "被拒绝", tone: "denied" })
  })
})

describe("formatToolDuration", () => {
  test("names the approval wait when a human dominated the call (A-10)", () => {
    expect(formatToolDuration(28_900, 28_500)).toBe("28.9s（审批 28.5s）")
  })

  test("a short wait or a minor share stays out of the row", () => {
    expect(formatToolDuration(820, 0)).toBe("820ms")
    expect(formatToolDuration(1500, 900)).toBe("1.5s")
    // 8% of the call: the wait is not what the reader is wondering about.
    expect(formatToolDuration(50_000, 4_000)).toBe("50.0s")
  })

  test("a bogus wait is clamped to the total", () => {
    expect(formatToolDuration(1500, 9000)).toBe("1.5s（审批 1.5s）")
    expect(formatToolDuration(1500, -1)).toBe("1.5s")
  })
})

describe("truncationNote", () => {
  test("says nothing when nothing was dropped", () => {
    expect(truncationNote(false)).toBe("")
  })

  test("says so when the head was dropped", () => {
    expect(truncationNote(true)).toContain("截断")
  })
})

/**
 * A5 T8/A7 T6: the live tool tail.  Chunks arrive mid-line, so a fold that
 * splits on every chunk would cut a line in half and then colour the second
 * half by whichever stream happened to deliver it.
 */
describe("foldToolChunk", () => {
  test("a chunk that does not end a line waits for the rest of it", () => {
    const first = foldToolChunk([], "", "err", "stderr")
    expect(first.lines).toEqual([])
    expect(first.pending).toBe("err")
    expect(first.dropped).toBe(false)

    const second = foldToolChunk(first.lines, first.pending, "or\n", "stderr")
    expect(second.lines).toEqual([{ text: "error", stream: "stderr" }])
    expect(second.pending).toBe("")
  })

  test("newline-separated chunks land as separate lines of their own stream", () => {
    const after = foldToolChunk([], "", "out\nerr\n", "stdout")
    expect(after.lines).toEqual([
      { text: "out", stream: "stdout" },
      { text: "err", stream: "stdout" },
    ])
  })

  test("stderr keeps its stream so the card can colour it", () => {
    const after = foldToolChunk([], "", "boom\n", "stderr")
    expect(after.lines).toEqual([{ text: "boom", stream: "stderr" }])
  })
})

describe("the tail budget", () => {
  test("keeps the newest lines and reports that older ones were dropped", () => {
    let lines: TailLine[] = []
    let pending = ""
    for (let i = 0; i < 400; i += 1) {
      // 15 chars + newline each, 400 lines = 6400 chars, over the 4000 cap
      const folded = foldToolChunk(lines, pending, `${String(i).padStart(4, "0")}aaaaaaaaaa\n`, "stdout")
      lines = folded.lines
      pending = folded.pending
      if (folded.dropped) dropped = true
    }
    expect(dropped).toBe(true)
    const kept = lines.reduce((sum, line) => sum + line.text.length + 1, 0)
    expect(kept).toBeLessThanOrEqual(TAIL_CHAR_LIMIT)
    // oldest first, so the head is what went
    expect(lines[0]!.text > lines[lines.length - 1]!.text).toBe(false)
    expect(lines[lines.length - 1]!.text.startsWith("0399")).toBe(true)
  })
})

describe("the open line's budget", () => {
  test("one long line with no newline is cut and says so", () => {
    // 4000 + 1500 with no newline anywhere: the old flat string kept the last
    // 4000; the tail of that has to stay visible instead of parking in pending.
    const first = foldToolChunk([], "", "x".repeat(4000), "stdout")
    const second = foldToolChunk(first.lines, first.pending, "y".repeat(1500), "stdout")
    expect(second.pending).toBe("")
    expect(second.lines).toHaveLength(1)
    expect(second.lines[0]!.text).toBe("x".repeat(2500) + "y".repeat(1500))
    expect(second.dropped).toBe(true) // the head of that line is gone
  })

  test("a short open line stays open and costs nothing", () => {
    const folded = foldToolChunk([{ text: "ok", stream: "stdout" }], "", "ty", "stderr")
    expect(folded.pending).toBe("ty")
    expect(folded.dropped).toBe(false)
  })
})
