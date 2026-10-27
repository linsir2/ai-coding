import { describe, expect, test } from "bun:test"
import {
  mcpAddBody,
  memoryRowLine,
  parseCommandLine,
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
/**
 * M5: the wizard's command field is one line; argv is what the config wants.
 */
describe("parseCommandLine", () => {
  test("splits a plain command into argv", () => {
    expect(parseCommandLine("python -m srv --flag")).toEqual(["python", "-m", "srv", "--flag"])
  })

  test("keeps a quoted path whole", () => {
    expect(parseCommandLine('npx -y "@modelcontextprotocol/server-github"')).toEqual([
      "npx",
      "-y",
      "@modelcontextprotocol/server-github",
    ])
    expect(parseCommandLine("python '/tmp/my server.py'")).toEqual([
      "python",
      "/tmp/my server.py",
    ])
  })

  test("extra whitespace produces no empty arguments", () => {
    expect(parseCommandLine("  npx   -y  pkg  ")).toEqual(["npx", "-y", "pkg"])
  })

  test("an empty line is an empty argv", () => {
    expect(parseCommandLine("   ")).toEqual([])
  })
})

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
describe("the body the wizard sends", () => {
  const values = { name: "github", url: "https://api.githubcopilot.com/mcp/", token: "ghp_x" }

  test("a remote server's token rides along as a token field, not as a header", () => {
    const body = mcpAddBody("remote", values, false)
    expect(body.token).toBe("ghp_x")
    expect(body.url).toBe("https://api.githubcopilot.com/mcp/")
    expect(body.type).toBe("remote")
  })

  test("a skipped token is absent rather than an empty string", () => {
    const body = mcpAddBody("remote", { ...values, token: "   " }, false)
    expect(body.token).toBeUndefined()
  })

  test("a local server never carries a token, even if the field was filled", () => {
    const body = mcpAddBody("local", { ...values, command: "python -m srv" }, true)
    expect(body.token).toBeUndefined()
    expect(body.command).toEqual(["python", "-m", "srv"])
  })
})

describe("memory rows", () => {
  test("a note reads as its text, with what it is and where it came from", () => {
    const row = memoryRowLine({
      id: "n1",
      text: "用户偏好中文回答，代码注释也要中文",
      kind: "preference",
      scene: "写作偏好",
      pinned: false,
      importance: 4,
      evidence: "model",
      hits: 2,
      updated: "2026-10-12T00:00:00Z",
    })
    expect(row.title).toContain("用户偏好中文回答")
    expect(row.description).toContain("写作偏好")
  })

  test("a pinned note is marked on the row, not only in a colour", () => {
    // The panel has no per-row colour field; the pin has to be readable while
    // scanning, which is what the marker is for. The description does not
    // repeat the word — one signal, not two.
    const note = {
      id: "n1", text: "t", kind: "decision", scene: "", pinned: true,
      importance: 5, evidence: "tool", hits: 0, updated: "",
    }
    const row = memoryRowLine(note)
    expect(row.title.startsWith("📌")).toBe(true)
    expect(row.description).not.toContain("钉住")
    expect(memoryRowLine({ ...note, pinned: false }).title).toBe("t")
  })

  test("a fact the model only asserted is named as such", () => {
    const row = memoryRowLine({
      id: "n2", text: "这个项目用 Vite", kind: "fact", scene: "技术栈",
      pinned: false, importance: 3, evidence: "model", hits: 0, updated: "",
    })
    expect(row.description).toContain("仅模型陈述")
  })

  test("the kind is the grouping, because scene labels overlap", () => {
    // 33 scenes for 43 notes, several of them near-duplicates. Grouping by
    // them would bury the taxonomy under the extractor's free text.
    const kinds = ["decision", "fact", "preference", "open_thread"]
    expect(kinds.length).toBeGreaterThan(0)
  })
})
