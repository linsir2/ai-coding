import { describe, expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { ComposerMeta } from "../src/component/composer-meta"

/**
 * 19 §3.1: the composer's metadata line leads with the mode tag — PLAN in the
 * plan colour, Build muted, and a missing key (old backend) reads as Build
 * (the same fail-safe direction as the stats payload itself).  Frame
 * assertions only; the colour lives in the theme.
 */
const render = async (mode?: "normal" | "plan") => {
  const setup = await testRender(
    () => <ComposerMeta mode={mode} metadata="qwen3.7-flash · working" />,
    { width: 80, height: 2 },
  )
  await setup.flush()
  await setup.waitForVisualIdle()
  const frame = setup.captureCharFrame()
  setup.renderer.destroy()
  return frame
}

describe("ComposerMeta mode tag (19 §3.1)", () => {
  test("build mode shows the Build tag before the metadata", async () => {
    const frame = await render("normal")
    expect(frame).toContain("Build · qwen3.7-flash · working")
  })

  test("plan mode shows the PLAN tag", async () => {
    const frame = await render("plan")
    expect(frame).toContain("PLAN · qwen3.7-flash · working")
    expect(frame).not.toContain("Build")
  })

  test("a missing mode reads as Build (old backend)", async () => {
    const frame = await render(undefined)
    expect(frame).toContain("Build · qwen3.7-flash · working")
  })
})
