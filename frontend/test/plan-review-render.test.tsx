import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { PlanReviewPanel } from "../src/component/plan-review"
import { setupKeymap } from "../src/keymap"
import type { AskRequest } from "../src/util/ask"

/**
 * 19 §3.2: the review card is what a plan submission looks like in the
 * composer's slot — title, question, the plan's opening lines, and the two
 * ways out.  Frame assertions only; the answer codec lives in ask.test.ts.
 */
const REQUEST: AskRequest = {
  question: "批准这个计划并开始执行？",
  options: ["开始执行", "继续规划"],
  detail: "# 编排层改造计划\n1. 读取模块\n2. 写测试\n3. 运行验证",
  intent: "plan_review",
}

const render = async () => {
  const base = await createTestRenderer({ width: 90, height: 20 })
  const setup = await testRender(
    () => (
      <KeymapProvider keymap={setupKeymap(base.renderer)}>
        <PlanReviewPanel request={REQUEST} onAnswer={() => {}} />
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

/** Join the wrapped rows so substring assertions survive line wrapping. */
const textOf = (frame: string): string => frame.split("\n").join(" ")

describe("PlanReviewPanel (19 §3.2)", () => {
  test("shows the title, the question, the plan excerpt and both options", async () => {
    const frame = textOf(await render())
    expect(frame).toContain("◆ Plan review")
    expect(frame).toContain("批准这个计划并开始执行？")
    expect(frame).toContain("# 编排层改造计划")
    expect(frame).toContain("1  开始执行")
    expect(frame).toContain("2  继续规划")
  })
})
