/**
 * TDD (M2): tool card metadata — a faithful port of the verb table and target
 * extraction the retired Textual transcript used (ui/blocks.py), minus
 * the `kind` labels: those only ever grouped folds, which the parts model
 * does not have.  What the card renders is exactly what is pinned here.
 */

import { describe, expect, test } from "bun:test"
import {
  formatDuration,
  toolFailure,
  toolTarget,
  toolTitle,
  toolVerb,
  truncationNote,
} from "../src/util/toolmeta"

describe("toolVerb", () => {
  test("known tools use the ported Chinese verbs", () => {
    expect(toolVerb("bash")).toBe("运行命令")
    expect(toolVerb("read")).toBe("读取文件")
    expect(toolVerb("edit")).toBe("编辑文件")
    expect(toolVerb("todo_write")).toBe("更新计划")
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

describe("truncationNote", () => {
  test("says nothing when nothing was dropped", () => {
    expect(truncationNote(false)).toBe("")
  })

  test("says so when the head was dropped", () => {
    expect(truncationNote(true)).toContain("截断")
  })
})
