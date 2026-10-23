import { describe, expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { StatusBar } from "../src/component/status-bar"

/**
 * P5 (18 §10.5) + T7: the status bar names the agent first — the posture
 * marker (PLAN/只读) is a suffix on that name, not the headline.  A normal
 * writable build session reads `Build`; an old backend without an agent
 * falls back the same way.  Frame assertions only (the colours live in the
 * theme, not in the captured text).
 */
const render = async (extra: {
  agent?: string | null
  agentName?: string | null
  mode?: "normal" | "plan"
  readonly?: boolean
}) => {
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

  // T7: the agent name leads, the posture suffixes it.
  test("a custom agent names itself before the posture suffix", async () => {
    const frame = await render({ agent: "reviewer", agentName: "Reviewer", mode: "normal" })
    expect(frame).toContain("Reviewer")
    expect(frame).not.toContain("PLAN")
  })

  test("the plan agent keeps its posture suffix after its name", async () => {
    const frame = await render({ agent: "plan", agentName: "Plan", mode: "plan", readonly: true })
    expect(frame).toContain("Plan·PLAN·只读")
  })
})
