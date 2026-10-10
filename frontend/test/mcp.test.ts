import { describe, expect, test } from "bun:test"
import { mcpStatusLabel, mcpStatusTone, mcpToolNote } from "../src/util/mcp"

/**
 * MCP-MCP T6: the panel's words.  Four states, and the only one that needs
 * care is `filtered` — R7d's whitelist dropped tools on purpose, so it must
 * not read as a failure.
 */
describe("mcpStatusLabel", () => {
  test("each state says what happened", () => {
    expect(mcpStatusLabel("connected")).toBe("Connected ✓")
    expect(mcpStatusLabel("failed")).toBe("Failed !")
    expect(mcpStatusLabel("filtered")).toBe("已过滤")
    expect(mcpStatusLabel("off")).toBe("未启用")
  })
})

describe("mcpStatusTone", () => {
  test("connected is success, failed is error, filtered is a warning", () => {
    // filtered: dropping tools was a deliberate saving of context, not a fault.
    expect(mcpStatusTone("connected")).toBe("success")
    expect(mcpStatusTone("failed")).toBe("error")
    expect(mcpStatusTone("filtered")).toBe("warn")
    expect(mcpStatusTone("off")).toBe("muted")
  })
})

describe("mcpToolNote", () => {
  const server = (over: Partial<Parameters<typeof mcpToolNote>[0]>) => ({
    name: "fs",
    command: "mcp-fs",
    status: "connected" as const,
    toolCount: 0,
    wantedCount: 0,
    detail: "",
    ...over,
  })

  test("no whitelist reads as a plain count", () => {
    expect(mcpToolNote(server({ toolCount: 3 }))).toBe("3 tools")
  })

  test("a whitelist that filtered reads as kept of asked", () => {
    expect(mcpToolNote(server({ status: "filtered", toolCount: 1, wantedCount: 1 }))).toBe(
      "1 / 1 tools",
    )
  })

  test("a server with nothing to show says so instead of 0 tools", () => {
    expect(mcpToolNote(server({ toolCount: 0 }))).toBe("")
  })
})