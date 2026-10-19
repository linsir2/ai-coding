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
  data: {},
}

// R04: a plan longer than the retired twelve-line excerpt, plus the rows
// approval will write (verify + dependency included).
const REQUEST_WITH_ITEMS: AskRequest = {
  question: "批准这个计划并开始执行？",
  options: ["开始执行", "继续规划"],
  detail: ["# 长计划", ...Array.from({ length: 14 }, (_, i) => `步骤 ${i + 1}`)].join("\n"),
  intent: "plan_review",
  data: {
    items: [
      { content: "建目录", verify: "test -d hotel", dependsOn: [] },
      { content: "写 db.py", verify: "", dependsOn: [1] },
    ],
  },
}

const render = async (request: AskRequest = REQUEST, height = 20) => {
  const base = await createTestRenderer({ width: 90, height })
  const setup = await testRender(
    () => (
      <KeymapProvider keymap={setupKeymap(base.renderer)}>
        <PlanReviewPanel request={request} onAnswer={() => {}} />
      </KeymapProvider>
    ),
    base,
  )
  // testRender builds its own renderer (its second argument is a config
  // object, and a TestRendererSetup carries no top-level width/height), so
  // the real viewport starts at the process default.  Resize it to what this
  // frame is supposed to measure -- useTerminalDimensions follows `resize`.
  setup.resize(90, height)
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

describe("PlanReviewPanel items (R04)", () => {
  test("renders the full plan and the items table", async () => {
    const frame = textOf(await render(REQUEST_WITH_ITEMS, 60))
    expect(frame).toContain("步骤 14")
    expect(frame).toContain("1. 建目录")
    expect(frame).toContain("2. 写 db.py")
    expect(frame).not.toContain("全文见上一条")
  })

  test("shows verify and dependency lines", async () => {
    const frame = textOf(await render(REQUEST_WITH_ITEMS, 60))
    expect(frame).toContain("verify：test -d hotel")
    expect(frame).toContain("无证明命令")
    expect(frame).toContain("依赖 #1")
  })

  test("no longer truncates at twelve lines", async () => {
    const frame = textOf(await render(REQUEST_WITH_ITEMS, 60))
    expect(frame).toContain("步骤 13")
    expect(frame).not.toContain("…（全文见上一条 present_plan 调用）")
  })
})
