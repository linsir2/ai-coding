import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { PermissionPanel } from "../src/component/permission"
import { setupKeymap } from "../src/keymap"

/**
 * The panel is where a remote tool's side effects are approved, so it is
 * asserted on a captured frame: an operator must be able to read *what the
 * call will do* before answering, not just which tool it names. The arguments
 * reached the frontend all along; this is the proof they are rendered.
 */

const render = async (request: {
  toolName: string
  params: Record<string, unknown>
  reason: string
}) => {
  const base = await createTestRenderer({ width: 80, height: 20 })
  const setup = await testRender(
    () => (
      <KeymapProvider keymap={setupKeymap(base.renderer)}>
        <PermissionPanel request={request} onDecide={() => {}} />
      </KeymapProvider>
    ),
    base,
  )
  await setup.flush()
  await setup.waitForVisualIdle()
  const frame = setup.captureCharFrame()
  setup.renderer.destroy()
  return frame
}

const textOf = (frame: string): string => frame.split("\n").join(" ")

describe("PermissionPanel", () => {
  test("an MCP approval shows the arguments it is about to run", async () => {
    const frame = textOf(
      await render({
        toolName: "fs__write_file",
        params: {
          server: "fs",
          tool: "write_file",
          input: { path: "/etc/passwd", content: "root::0:0:::/root:/bin/sh" },
        },
        reason: "unknown tool 'fs__write_file' requires confirmation",
      }),
    )

    expect(frame).toContain("fs__write_file")
    expect(frame).toContain("/etc/passwd")
    expect(frame).toContain("root::0:0")
  })

  test("a long argument is cut rather than wrapped over the answer keys", async () => {
    const frame = await render({
      toolName: "fs__write_file",
      params: { server: "fs", tool: "write_file", input: { content: "x".repeat(400) } },
      reason: "why",
    })
    const rows = frame.split("\n").filter((row) => row.includes("content:"))

    expect(rows).toHaveLength(1)
    expect(rows[0]!.length).toBeLessThanOrEqual(80)
    expect(rows[0]).toContain("…")
  })

  test("the answer keys are still on screen underneath the payload", async () => {
    const frame = await render({
      toolName: "fs__many",
      params: {
        server: "fs",
        tool: "many",
        input: Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`k${i}`, `value ${i}`])),
      },
      reason: "why",
    })

    expect(frame).toContain("Allow once")
    expect(frame).toContain("Reject")
  })

  test("a local tool keeps its one-line summary and gains nothing", async () => {
    const frame = textOf(
      await render({ toolName: "bash", params: { command: "rm -rf /", timeout: 10 }, reason: "why" }),
    )

    expect(frame).toContain("rm -rf /")
    expect(frame).not.toContain("timeout")
  })
})