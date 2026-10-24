import { describe, expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { ComposerMeta } from "../src/component/composer-meta"

/**
 * 19 §3.1 + T7: the composer's metadata line leads with the agent name — the
 * plan colour for plan, muted otherwise — and PLAN/只读 is a suffix, not the
 * headline.  A missing agent (old backend) reads as Build (the same
 * fail-safe direction as the stats payload itself).  Frame assertions only;
 * the colour lives in the theme.
 */
const render = async (props: {
  agent?: string | null
  agentName?: string | null
  mode?: "normal" | "plan"
  readonly?: boolean
}) => {
  const setup = await testRender(
    () => <ComposerMeta {...props} metadata="qwen3.7-flash · working" />,
    { width: 80, height: 2 },
  )
  await setup.flush()
  await setup.waitForVisualIdle()
  const frame = setup.captureCharFrame()
  setup.renderer.destroy()
  return frame
}

describe("ComposerMeta agent tag (19 §3.1, T7)", () => {
  test("build mode shows the agent name before the metadata", async () => {
    const frame = await render({ agent: "build", agentName: "Build", mode: "normal" })
    expect(frame).toContain("Build · qwen3.7-flash · working")
  })

  // The plan agent is already named "Plan" — the suffix keeps only what
  // the name does not say ("只读"), so "Plan·PLAN·只读" never renders.
  test("plan mode shows the agent name with readonly as suffix", async () => {
    const frame = await render({ agent: "plan", agentName: "Plan", mode: "plan", readonly: true })
    expect(frame).toContain("Plan · 只读 · qwen3.7-flash · working")
    expect(frame).not.toContain("PLAN")
    expect(frame).not.toContain("Build")
  })

  test("a non-plan agent in plan posture keeps the PLAN suffix", async () => {
    const frame = await render({ agent: "reviewer", agentName: "Reviewer", mode: "plan", readonly: true })
    expect(frame).toContain("Reviewer · PLAN·只读 · qwen3.7-flash · working")
  })

  test("a custom agent names itself, posture still suffixes", async () => {
    const frame = await render({ agent: "reviewer", agentName: "Reviewer", mode: "normal" })
    expect(frame).toContain("Reviewer · qwen3.7-flash · working")
    expect(frame).not.toContain("PLAN")
  })

  test("a missing agent reads as Build (old backend)", async () => {
    const frame = await render({})
    expect(frame).toContain("Build · qwen3.7-flash · working")
  })
})
