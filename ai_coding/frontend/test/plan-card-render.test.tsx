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
  items: { id: string; content: string; status: string; verify?: string; dependsOn?: string[] }[],
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

  test("items show their proof command and dependencies (D5)", async () => {
    // D5: the live card shows what approval will run -- the same second
    // line the review card draws, so the two cards can be checked against
    // each other. An empty verify reads "无证明命令" (never a missing line).
    const setup = await render(
      planMessage("上线", [
        { id: "1", content: "建目录", status: "completed", verify: "test -d out" },
        { id: "2", content: "写文件", status: "in_progress", verify: "", dependsOn: ["1"] },
        { id: "3", content: "想事情", status: "pending" },
      ]),
    )
    const frame = setup.captureCharFrame()
    const text = frame.split("\n").join(" ")
    expect(text).toContain("verify：test -d out")
    expect(text).toContain("verify：无证明命令 · 依赖 #1")
    expect(text).toContain("verify：无证明命令")
    setup.renderer.destroy()
  })
})
