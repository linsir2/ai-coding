import { describe, expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { StatusBar } from "../src/component/status-bar"

/**
 * P5 (18 §10.5): the status bar carries the posture marker — one chip for the
 * prompt axis, one for the enforcement axis, combined when both are on; a
 * normal writable session shows nothing.  Frame assertions only (the muted
 * colour lives in the theme, not in the captured text).
 */
const render = async (extra: { mode?: "normal" | "plan"; readonly?: boolean }) => {
  const setup = await testRender(
    () => (
      <StatusBar
        workspace="/home/u/proj"
        branch="main"
        tokens={13930}
        maxContextTokens={48000}
        version="0.1.0"
        {...extra}
      />
    ),
    { width: 120, height: 3 },
  )
  await setup.flush()
  await setup.waitForVisualIdle()
  const frame = setup.captureCharFrame()
  setup.renderer.destroy()
  return frame
}

describe("StatusBar posture marker (P5)", () => {
  test("normal and writable shows no marker", async () => {
    const frame = await render({})
    expect(frame).not.toContain("PLAN")
    expect(frame).not.toContain("只读")
  })

  test("plan only shows PLAN", async () => {
    const frame = await render({ mode: "plan", readonly: false })
    expect(frame).toContain("PLAN")
    expect(frame).not.toContain("只读")
  })

  test("readonly only shows the readonly chip", async () => {
    const frame = await render({ mode: "normal", readonly: true })
    expect(frame).toContain("只读")
    expect(frame).not.toContain("PLAN")
  })

  test("plan plus readonly shows the combined marker", async () => {
    const frame = await render({ mode: "plan", readonly: true })
    expect(frame).toContain("PLAN·只读")
  })
})
