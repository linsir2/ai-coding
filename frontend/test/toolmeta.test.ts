/**
 * TDD (M2): tool card metadata — a faithful port of the verb table and target
 * extraction the old Textual transcript used (ai_coding/ui/blocks.py), minus
 * the `kind` labels: those only ever grouped folds, which the parts model
 * does not have.  What the card renders is exactly what is pinned here.
 */

import { describe, expect, test } from "bun:test"
import { toolTarget, toolTitle, toolVerb } from "../src/util/toolmeta"

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
