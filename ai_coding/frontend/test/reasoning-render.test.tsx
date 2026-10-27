import { describe, expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { Reasoning } from "../src/component/reasoning"

/**
 * A4 T10, for real this time: OpenTUI ships its own test renderer
 * (`@opentui/solid`'s `testRender` + `@opentui/core/testing`'s mock mouse), so
 * the fold, its headline and the click toggle are asserted on a captured frame
 * instead of eyeballed through tmux.
 */
const REASONING = "用户想知道当前分支。\n先跑 git branch --show-current 看看。"

const frame = async (setup: Awaited<ReturnType<typeof testRender>>) => {
  await setup.flush()
  await setup.waitForVisualIdle()
  return setup.captureCharFrame()
}

describe("Reasoning", () => {
  test("folded: one marker line plus the first sentence", async () => {
    const setup = await testRender(() => <Reasoning text={REASONING} alwaysOpen={false} />, {
      width: 60,
      height: 12,
    })
    const rendered = await frame(setup)
    expect(rendered).toContain("+ Thought")
    // Only the first sentence rides the folded line; the rest is behind the fold.
    expect(rendered).toContain("用户想知道当前分支。")
    expect(rendered).not.toContain("先跑 git branch")
    setup.renderer.destroy()
  })

  test("a click on the marker opens the thought, a second click closes it", async () => {
    const setup = await testRender(() => <Reasoning text={REASONING} alwaysOpen={false} />, {
      width: 60,
      height: 12,
    })
    await frame(setup)

    // Click the marker where it actually is in each state: folded it starts at
    // column 4, open it sits after the border at column 6.
    await setup.mockMouse.click(4, 1)
    expect(await frame(setup)).toContain("- Thought")

    await setup.mockMouse.click(6, 1)
    expect(await frame(setup)).toContain("+ Thought")
    setup.renderer.destroy()
  })

  test("alwaysOpen pins it open and the click does nothing", async () => {
    const setup = await testRender(() => <Reasoning text={REASONING} alwaysOpen={true} />, {
      width: 60,
      height: 12,
    })
    const opened = await frame(setup)
    expect(opened).toContain("- Thought")

    await setup.mockMouse.click(4, 1)
    const still = await frame(setup)
    expect(still).toContain("- Thought")
    setup.renderer.destroy()
  })
})

/**
 * A4 T11 (verified in the app, not here): when `message.finished` turns the
 * streaming text part at position 0 into a reasoning one, `<Index>` reuses that
 * instance, so the branch has to be reactive.  This harness renders through
 * solid's SERVER build (`solid-js/dist/server.js`), where an out-of-band signal
 * write never schedules a re-render — so the case is covered by the tmux run in
 * docs/opencode-gui-a4-design.md §8 instead of by a fake test here.
 */
