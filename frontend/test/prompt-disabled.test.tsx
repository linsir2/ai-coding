/**
 * D4: pausing the composer must not throw the draft away.
 *
 * The card used to unmount the textarea, so half a sentence the operator was
 * typing disappeared the moment a permission asked.  `disabled` keeps the
 * renderable (and its text) in place and only takes the keyboard away.
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { createSignal } from "solid-js"
import { Prompt } from "../src/component/prompt"
import { setupKeymap } from "../src/keymap"

describe("Prompt disabled (D4)", () => {
  test("the draft survives the card and comes back with the composer", async () => {
    const base = await createTestRenderer({ width: 100, height: 20 })
    const keymap = setupKeymap(base.renderer)
    const disabled = createSignal(false)
    const setup = await testRender(
      () => (
        <KeymapProvider keymap={keymap}>
          <Prompt
            placeholder="Ask anything…"
            metadata="p · m"
            seed="半句需求"
            onSubmit={() => {}}
            disabled={disabled[0]()}
          />
        </KeymapProvider>
      ),
      base,
    )
    await setup.flush()
    await setup.waitForVisualIdle()
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")
    expect(frameOf()).toContain("半句需求")

    // the pause must not clear the box...
    disabled[1](true)
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("半句需求")

    // ...and the composer comes back with the same draft
    disabled[1](false)
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("半句需求")

    setup.renderer.destroy()
  })
})
