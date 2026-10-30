import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { ASK_OTHER_OPTION, AskPanel } from "../src/component/ask"
import { setupKeymap } from "../src/keymap"
import type { AskRequest } from "../src/util/ask"

/**
 * C3: the question's choices are the model's, so they can never be complete.
 * The panel therefore adds one free-text row to every option list; without
 * options it keeps the plain input.
 */
const request = (options: string[]): AskRequest => ({
  question: "这份计划哪里需要调整？",
  options,
  detail: "",
  intent: "",
  data: {},
})

const render = async (req: AskRequest, height = 16) => {
  const base = await createTestRenderer({ width: 90, height })
  const setup = await testRender(
    () => (
      <KeymapProvider keymap={setupKeymap(base.renderer)}>
        <AskPanel request={req} onAnswer={() => {}} />
      </KeymapProvider>
    ),
    base,
  )
  setup.resize(90, height)
  await setup.flush()
  await setup.waitForVisualIdle()
  const frame = setup.captureCharFrame()
  setup.renderer.destroy()
  return frame.split("\n").join(" ")
}

describe("AskPanel (C3: options plus a free-text row)", () => {
  test("keeps the model's choices and appends the free-text row", async () => {
    const frame = await render(request(["拆得更细", "调整顺序与依赖"]))

    expect(frame).toContain("◆ Question")
    expect(frame).toContain("这份计划哪里需要调整？")
    expect(frame).toContain("1  拆得更细")
    expect(frame).toContain("2  调整顺序与依赖")
    expect(frame).toContain(`3  ${ASK_OTHER_OPTION}`)
  })

  test("without options the panel is the plain input", async () => {
    const frame = await render(request([]))

    expect(frame).not.toContain(ASK_OTHER_OPTION)
    expect(frame).toContain("Type your answer")
  })
})
