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
  items: { id: string; content: string; status: string }[],
): ChatMessage => ({ role: "assistant", parts: [{ kind: "plan", goal, items }] })

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
})
