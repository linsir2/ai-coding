import { describe, expect, test } from "bun:test"
import {
  mcpRowCanConnect,
  mcpRowCanLogin,
  mcpStatusLabel,
  mcpStatusTone,
  mcpToolNote,
} from "../src/util/mcp"

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
    expect(mcpStatusLabel("connecting")).toBe("Connecting …")
    expect(mcpStatusLabel("needs_auth")).toBe("Sign in required →")
  })
})

describe("mcpStatusTone", () => {
  test("connected is success, failed is error, filtered is a warning", () => {
    // filtered: dropping tools was a deliberate saving of context, not a fault.
    expect(mcpStatusTone("connected")).toBe("success")
    expect(mcpStatusTone("failed")).toBe("error")
    expect(mcpStatusTone("filtered")).toBe("warn")
    expect(mcpStatusTone("off")).toBe("muted")
    expect(mcpStatusTone("connecting")).toBe("muted")
    expect(mcpStatusTone("needs_auth")).toBe("warn")
  })
})

/**
 * M3: the panel's one action.  Exactly one state offers it, and the interesting
 * half is the three that do not: retrying a connected server is theatre,
 * retrying a disabled one is impossible, and retrying a connecting one
 * duplicates what is already happening.
 */
describe("mcpRowCanConnect", () => {
  test("only a failed row can be retried", () => {
    expect(mcpRowCanConnect("failed")).toBe(true)
    expect(mcpRowCanConnect("connected")).toBe(false)
    expect(mcpRowCanConnect("filtered")).toBe(false)
    expect(mcpRowCanConnect("off")).toBe(false)
    expect(mcpRowCanConnect("connecting")).toBe(false)
  })
})

/**
 * M4: the other waiting state gets the *other* action.  A needs_auth server
 * answered 401, so re-running a connect would only 401 again — the row's
 * action starts a sign-in instead.
 */
describe("mcpRowCanLogin", () => {
  test("only a needs_auth row can be signed in", () => {
    expect(mcpRowCanLogin("needs_auth")).toBe(true)
    expect(mcpRowCanLogin("failed")).toBe(false)
    expect(mcpRowCanLogin("connected")).toBe(false)
    expect(mcpRowCanLogin("filtered")).toBe(false)
    expect(mcpRowCanLogin("off")).toBe(false)
    expect(mcpRowCanLogin("connecting")).toBe(false)
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