/**
 * E1: the posture toggle must be visible before the first submission.
 *
 * Home carries shift+tab because the Session route does not exist yet at
 * cold start (20 §批次1), but nothing on screen said the key existed -- the
 * only feedback was the status row turning to PLAN.  The hint sits under the
 * composer and follows the mode, so entering and leaving are both taught.
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogProvider } from "../src/dialog/context"
import { Home } from "../src/routes/home"
import { setupKeymap } from "../src/keymap"

describe("Home posture hint (E1)", () => {
  test("the row names shift+tab and follows the mode", async () => {
    const base = await createTestRenderer({ width: 100, height: 40 })
    const keymap = setupKeymap(base.renderer)
    const setup = await testRender(
      () => (
        <KeymapProvider keymap={keymap}>
          <DialogProvider renderer={base.renderer}>
            <Home
              workspace="/tmp/ws"
              version="0.1.0"
              model="m"
              provider="p"
              branch=""
              onSubmit={() => {}}
              client={{} as never}
              skills={[]}
              files={[]}
              commands={["plan", "help"]}
            />
          </DialogProvider>
        </KeymapProvider>
      ),
      base,
    )
    await setup.flush()
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")
    expect(frameOf()).toContain("shift+tab")
    expect(frameOf()).toContain("计划模式")

    keymap.runCommand("home.mode.cycle")
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("shift+tab")
    expect(frameOf()).toContain("退出计划")

    setup.renderer.destroy()
  })
})
