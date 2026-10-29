import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { createTestKeymap } from "@opentui/keymap/testing"
import { testRender } from "@opentui/solid"
import {
  PlanReviewPanel,
  REVIEW_KEY_BINDINGS,
} from "../src/component/plan-review"
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

// A（审核修复）：12 条目 + 长正文 = 24 行真实终端里视口放不下，正是缺陷现场。
const REQUEST_WITH_MANY_ITEMS: AskRequest = {
  question: "批准这个计划并开始执行？",
  options: ["开始执行", "继续规划"],
  detail: ["# 长计划", ...Array.from({ length: 14 }, (_, i) => `步骤 ${i + 1}`)].join("\n"),
  intent: "plan_review",
  data: {
    items: Array.from({ length: 12 }, (_, i) => ({
      content: `步骤 ${i + 1}`,
      verify: `echo verify-${i + 1}`,
      dependsOn: i > 0 ? [i] : [],
    })),
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
    // R21 §1: the card shows the plan through planText, so the ``#`` heading
    // marker is gone -- the words are what the operator reads.
    expect(frame).toContain("编排层改造计划")
    expect(frame).not.toContain("# 编排层改造计划")
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

describe("PlanReviewPanel intent (C: 判定收敛到按钮)", () => {
  test("approve is approve; keep and esc are both dismiss (never arm)", () => {
    const harness = createTestKeymap({ defaultKeys: true })
    const seen: string[] = []
    harness.keymap.registerLayer({
      commands: [
        {
          name: "plan-review.approve",
          desc: "",
          run: () => seen.push("approve"),
        },
        { name: "plan-review.keep", desc: "", run: () => seen.push("keep") },
        { name: "plan-review.dismiss", desc: "", run: () => seen.push("dismiss") },
      ],
      // The component's own key table, imported -- not a copy of it: a copy
      // would test the copy.
      bindings: REVIEW_KEY_BINDINGS.filter((b) =>
        ["1", "2", "escape"].includes(b.key),
      ) as never,
    })
    harness.host.press("1")
    harness.host.press("2")
    harness.host.press("escape")
    expect(seen).toEqual(["approve", "keep", "dismiss"])
    harness.cleanup()
  })
})

describe("PlanReviewPanel scroll reachability (A)", () => {
  test("the pager and half-page keys are bound to the scroll commands", () => {
    const harness = createTestKeymap({ defaultKeys: true })
    const scrolled: number[] = []
    harness.keymap.registerLayer({
      commands: [
        {
          name: "plan-review.scroll-down",
          desc: "",
          run: () => scrolled.push(1),
        },
        {
          name: "plan-review.scroll-up",
          desc: "",
          run: () => scrolled.push(-1),
        },
      ],
      bindings: REVIEW_KEY_BINDINGS.filter((b) => b.cmd.startsWith("plan-review.scroll")) as never,
    })
    harness.host.press("pagedown")
    harness.host.press("d", { ctrl: true })
    harness.host.press("pageup")
    harness.host.press("u", { ctrl: true })
    expect(scrolled).toEqual([1, 1, -1, -1])
    harness.cleanup()
  })

  test("at a real 24-row viewport the footer names the scroll keys", async () => {
    // The 60-row frames the other tests use hid the bug: the region is only
    // floor(h/2)-6 rows, so 12 items did not fit in a 24-row terminal and
    // nothing could scroll them into view.
    const frame = textOf(await render(REQUEST_WITH_MANY_ITEMS, 24))
    expect(frame).toContain("pgup/pgdn")
    expect(frame).toContain("滚动")
  })

  test("the region scrolls: more items exist below the fold than fit on screen", async () => {
    // Measured, not assumed: at 60 rows the region is 24 lines and a 15-line
    // plan plus 12 two-line items cannot all fit -- only the first item shows.
    // That overflow is exactly why A exists, and why the scroll keys matter
    // more than any reordering of the content.
    const frame = await render(REQUEST_WITH_MANY_ITEMS, 60)
    expect(frame).toContain("1. 步骤 1")
    // 15 lines of plan prose + 4 item rows made it into the 24-line region;
    // the other 8 items are below the fold and only a scroll reaches them.
    const rows = frame.split("\n").filter((l) => /^\s*┃\s*\d+\.\s步骤/.test(l))
    expect(rows.length).toBe(4)
    expect(frame).not.toContain("12. 步骤 12")
  })
})

describe("PlanReviewPanel dismiss label (R08)", () => {
  test("esc is labelled as a dismiss that ends the turn", async () => {
    const frame = textOf(await render())
    // The footer is one wrapped line, so assert on the wrap-safe fragments.
    expect(textOf(frame)).toContain("esc 结束本")
    expect(frame).not.toContain("esc hold")
  })
})
