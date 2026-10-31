/**
 * 20 §批次2: the plan card is the ONLY place the todo list lives now -- a
 * settled todo_write card and the raw `<task-state>` dump are gone, so this
 * pins the card's OpenCode-style checkboxes and status colours.
 */

import { describe, expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { MessageView } from "../src/component/message"
import type { ChatMessage } from "../src/state/session"

const planMessage = (
  goal: string,
  items: {
    id: string
    content: string
    status: string
    verify?: string
    dependsOn?: string[]
    evidence?: string
  }[],
): ChatMessage => ({
  role: "assistant",
  parts: [
    {
      kind: "plan",
      goal,
      items: items.map((item) => ({ verify: "", dependsOn: [], ...item })),
    },
  ],
})

const render = async (message: ChatMessage) => {
  const setup = await testRender(() => <MessageView message={message} alwaysOpen={false} />, {
    width: 80,
    height: 12,
  })
  await setup.flush()
  await setup.waitForVisualIdle()
  return setup
}

describe("the plan card (20 §批次2)", () => {
  test("draws the checklist with one marker per status and the progress count", async () => {
    const setup = await render(
      planMessage("探索一下你的计划层", [
        { id: "1", content: "读入口", status: "completed" },
        { id: "2", content: "写用例", status: "in_progress" },
        { id: "3", content: "改渲染", status: "pending" },
        { id: "4", content: "被依赖卡住", status: "blocked" },
      ]),
    )
    const frame = setup.captureCharFrame()
    expect(frame).toContain("# Todos 1/4")
    expect(frame).toContain("目标: 探索一下你的计划层")
    expect(frame).toContain("[✓] 1. 读入口")
    expect(frame).toContain("[•] 2. 写用例")
    expect(frame).toContain("[ ] 3. 改渲染")
    expect(frame).toContain("[!] 4. 被依赖卡住")
    expect(frame).not.toContain("<task-state>")
    setup.renderer.destroy()
  })

  test("an unknown status falls back to the pending marker", async () => {
    const setup = await render(
      planMessage("", [{ id: "1", content: "怪状态", status: "mystery" }]),
    )
    const frame = setup.captureCharFrame()
    expect(frame).toContain("[ ] 1. 怪状态")
    setup.renderer.destroy()
  })

  test("a skipped item is a muted [~] and the header counts it (r24/C)", async () => {
    const setup = await render(
      planMessage("", [
        { id: "1", content: "完成", status: "completed" },
        { id: "2", content: "依赖失败跳过", status: "skipped" },
      ]),
    )
    const frame = setup.captureCharFrame()
    expect(frame).toContain("# Todos 1/2 · 跳过 1")
    expect(frame).toContain("[~] 2. 依赖失败跳过")
    setup.renderer.destroy()
  })

  test("a live wave names itself and the items it covers (C3)", async () => {
    const setup = await render({
      role: "assistant",
      parts: [
        {
          kind: "plan",
          goal: "",
          items: [
            { id: "1", content: "当前波", status: "in_progress", verify: "", dependsOn: [] },
            { id: "2", content: "当前波续", status: "pending", verify: "", dependsOn: [] },
          ],
          rounds: 1,
          wave: { active: true, items: ["1", "2"] },
        },
      ],
    })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("# Todos 0/2 · 第 2 波 · #1,#2 进行中")
    setup.renderer.destroy()
  })

  test("between waves the card shows the completed count only (C3)", async () => {
    const setup = await render({
      role: "assistant",
      parts: [
        {
          kind: "plan",
          goal: "",
          items: [{ id: "1", content: "完成", status: "completed", verify: "", dependsOn: [] }],
          rounds: 3,
        },
      ],
    })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("# Todos 1/1 · 3 波")
    expect(frame).not.toContain("进行中")
    setup.renderer.destroy()
  })

  test("the live card keeps the proof command out of settled rows (D5, revised)", async () => {
    // D5's safety property lives in the review card (the approval screen
    // draws every verify in full).  The live card is for scanning progress:
    // settled rows are one line, and only the item running now shows a
    // truncated proof.  Measured 2026-10-31: a 14-item plan with full
    // `test -f && wc -l` lines filled the whole screen.
    const setup = await render(
      planMessage("上线", [
        { id: "1", content: "建目录", status: "completed", verify: "test -d out" },
        {
          id: "2",
          content: "跑全量测试",
          status: "in_progress",
          verify: "pytest tests/ -q 2>/dev/null",
          dependsOn: ["1"],
        },
        { id: "3", content: "想事情", status: "pending" },
      ]),
    )
    const frame = setup.captureCharFrame()
    const text = frame.split("\n").join(" ")
    expect(text).not.toContain("verify：test -d out")
    expect(text).not.toContain("依赖 #1")
    expect(text).toContain("verify: pytest tests/ -q 2>/dev/null")
    setup.renderer.destroy()
  })

  test("a blocked item shows why it could not land", async () => {
    const setup = await render(
      planMessage("上线", [
        {
          id: "1",
          content: "被依赖卡住",
          status: "blocked",
          evidence: "verify failed: assert 245 == 200",
        },
      ]),
    )
    const frame = setup.captureCharFrame()
    expect(frame).toContain("↳ verify failed: assert 245 == 200")
    setup.renderer.destroy()
  })
})
