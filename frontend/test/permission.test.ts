/**
 * TDD (M5): the permission panel's pure half — the option table.
 *
 * Labels are OpenCode's verbatim (`permissionOptionLabel`,
 * util/permission.ts:162); the decisions are the CLI vocabulary the shared
 * backend parser already speaks (security/approval.py:88-98).
 */

import { describe, expect, test } from "bun:test"
import { PERMISSION_OPTIONS, permissionOptions, permissionPayload, permissionRequest } from "../src/util/permission"

describe("permission options", () => {
  test("offers once, always, reject and stop with OpenCode's labels", () => {
    // Migration (P0-4B): the fourth option is new; the first three are
    // untouched, which is what this assertion is here to notice.
    expect(PERMISSION_OPTIONS.map((option) => option.label)).toEqual([
      "Allow once",
      "Always allow",
      "Reject",
      "Stop this turn",
    ])
  })

  test("stopping the turn survives an unrememberable call", () => {
    // "stop" is not about remembering anything, so the grantable filter must
    // not take it away -- otherwise the operator loses the precise answer
    // exactly on the calls that are asked most often.
    expect(permissionOptions({ grantable: false }).map((option) => option.decision)).toEqual([
      "yes",
      "no",
      "stop",
    ])
  })

  test("an always the gate cannot remember is not offered at all", () => {
    // grant_key is the recorder's own rule; for bash (and any call naming no
    // single resource) it returns None, so "always" would approve once and
    // forget -- an empty promise the panel must not make.
    expect(permissionOptions({ grantable: false }).map((option) => option.decision)).toContain(
      "no",
    )
  })

  test("a rememberable call keeps the shared option table", () => {
    expect(permissionOptions({})).toBe(PERMISSION_OPTIONS)
    expect(permissionOptions({ grantable: true })).toBe(PERMISSION_OPTIONS)
  })

  test("the wire payload keeps the gate's answer, and a missing one stays missing", () => {
    // Found live: the field reached the wire and the panel still offered
    // "always", because nobody copied it into the request. The mapper is the fix
    // and the reason it is a function.
    const notRememberable = permissionRequest({
      toolName: "bash",
      params: { command: "ls" },
      reason: "warn",
      grantable: false,
    })
    expect(notRememberable.grantable).toBe(false)
    // Migration (P0-4B): "always" is what drops out; "stop" is not about
    // remembering anything, so it stays.
    expect(permissionOptions(notRememberable).map((o) => o.decision)).toEqual([
      "yes",
      "no",
      "stop",
    ])

    const rememberable = permissionRequest({
      toolName: "write",
      params: { path: "a.py" },
      reason: "warn",
      grantable: true,
    })
    expect(rememberable.grantable).toBe(true)
    expect(permissionOptions(rememberable)).toBe(PERMISSION_OPTIONS)

    // An older backend sends nothing, which means "offer everything" -- not
    // "nothing can be remembered".
    expect(permissionRequest({ toolName: "bash", reason: "warn" }).grantable).toBeUndefined()
  })

  test("every option decodes to a decision the backend parser accepts", () => {
    const accepted = new Set(["yes", "always", "no", "stop"])
    for (const option of PERMISSION_OPTIONS) {
      expect(accepted.has(option.decision)).toBe(true)
    }
  })

  test("esc's fail-closed answer exists as the reject decision", () => {
    expect(PERMISSION_OPTIONS.find((option) => option.key === "reject")?.decision).toBe("no")
  })
})

describe("what the approval shows about an MCP call", () => {
  const call = {
    server: "fs",
    tool: "write_file",
    input: { path: "/etc/passwd", content: "root::0:0" },
  }

  test("shows the arguments — approving blind is the hole this closes", () => {
    const lines = permissionPayload({ toolName: "fs__write_file", params: call })
    expect(lines.join("\n")).toContain("/etc/passwd")
    expect(lines.join("\n")).toContain("root::0:0")
  })

  test("a scalar argument is shown, not JSON-dumped", () => {
    const lines = permissionPayload({ toolName: "fs__echo", params: { server: "fs", tool: "echo", input: "ping" } })
    expect(lines.join("\n")).toContain("ping")
  })

  test("a huge argument is cut, and says it was cut", () => {
    const lines = permissionPayload({
      toolName: "fs__write_file",
      params: { server: "fs", tool: "w", input: { blob: "x".repeat(5000) } },
    })
    const text = lines.join("\n")
    expect(text.length).toBeLessThan(1200)
    expect(text).toContain("…")
  })

  test("many arguments do not scroll the panel away", () => {
    const input: Record<string, string> = {}
    for (let i = 0; i < 40; i += 1) input[`k${i}`] = `v${i}`
    const lines = permissionPayload({ toolName: "fs__many", params: { server: "fs", tool: "many", input } })
    expect(lines.length).toBeLessThanOrEqual(12)
  })

  test("a local tool's own salient text is not duplicated into the payload", () => {
    // read/write/edit already name their path and bash names its command via
    // toolTarget; showing the whole params object there would be noise.
    expect(permissionPayload({ toolName: "write", params: { path: "a.py", content: "x" } })).toEqual([])
    expect(permissionPayload({ toolName: "bash", params: { command: "ls -la", timeout: 10 } })).toEqual([])
  })

  test("nothing to show is an empty block, not a blank line", () => {
    expect(permissionPayload({ toolName: "fs__echo", params: { server: "fs", tool: "echo", input: {} } })).toEqual([])
    expect(permissionPayload({ toolName: "todo_write", params: {} })).toEqual([])
  })
})
