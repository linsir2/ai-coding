/**
 * F2: the compaction card is a one-line status, not a second transcript.
 *
 * Measured trace 2026-10-31 (t8): ten compactions rendered ten full summary
 * blocks in the conversation.  The summary belongs to the model's working
 * context; the card only says what happened (counts, tokens, layers).
 */

import { describe, expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { MessageView } from "../src/component/message"
import type { ChatMessage } from "../src/state/session"

const message = (): ChatMessage => ({
  role: "assistant",
  parts: [
    {
      kind: "compaction",
      status: "done",
      reason: "auto",
      text: "## 目标\nSECRET-SUMMARY-BODY\n## 下一步\n继续",
      beforeMessages: 146,
      afterMessages: 14,
      inputTokens: 12_345,
      outputTokens: 1_234,
      totalTokens: 13_579,
      error: "",
      layers: [],
    },
  ],
})

describe("the compaction card (F2)", () => {
  test("shows the counts and tokens but never the summary body", async () => {
    const setup = await testRender(() => <MessageView message={message()} alwaysOpen={false} />, {
      width: 100,
      height: 10,
    })
    await setup.flush()
    await setup.waitForVisualIdle()
    const frame = setup.captureCharFrame()

    expect(frame).toContain("── 压缩")
    expect(frame).toContain("146→14 条")
    expect(frame).not.toContain("SECRET-SUMMARY-BODY")
    expect(frame).not.toContain("## 目标")
    setup.renderer.destroy()
  })
})
