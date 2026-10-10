import { describe, expect, test } from "bun:test"
import { TextAttributes } from "@opentui/core"
import { testRender } from "@opentui/solid"
import { ToolCard } from "../src/component/toolcard"
import type { Part } from "../src/state/session"

/**
 * A5 T5-T7: the card is the only place a failure reaches the human, so it is
 * asserted on a captured frame — the machine code must be gone, the duration
 * must be there, the row must open, and a denial must read as struck through.
 */
type ToolPart = Extract<Part, { kind: "tool" }>

const part = (over: Partial<ToolPart> = {}): ToolPart => ({
  kind: "tool",
  callId: "c1",
  toolName: "bash",
  verb: "运行命令",
  target: "git branch",
  status: "failed",
  lines: [],
  pending: "",
  linesDropped: false,
  preview: "",
  error: "",
  durationMs: 0,
  ...over,
})

const render = async (value: ToolPart) => {
  const setup = await testRender(() => <ToolCard part={value} />, { width: 70, height: 16 })
  await setup.flush()
  await setup.waitForVisualIdle()
  return setup
}

describe("ToolCard", () => {
  test("a failure reads as a sentence, never as the machine code", async () => {
    const setup = await render(part({ error: "nonzero_exit", durationMs: 820 }))
    const frame = setup.captureCharFrame()
    expect(frame).toContain("命令返回非零")
    expect(frame).not.toContain("nonzero_exit")
    expect(frame).toContain("820ms")
    setup.renderer.destroy()
  })

  test("an unknown code keeps itself visible", async () => {
    const setup = await render(part({ error: "brand_new_code" }))
    expect(setup.captureCharFrame()).toContain("brand_new_code")
    setup.renderer.destroy()
  })

  test("a denial is struck through", async () => {
    const setup = await render(part({ verb: "编辑文件", target: "a.py", error: "denied" }))
    const spans = setup.captureSpans()
    const struck = spans.lines.some((line) =>
      line.spans.some(
        (span) =>
          (span.attributes & TextAttributes.STRIKETHROUGH) !== 0 && span.text.includes("编辑文件"),
      ),
    )
    expect(struck).toBe(true)
    expect(setup.captureCharFrame()).toContain("被拒绝")
    setup.renderer.destroy()
  })


  test("stderr lines read as errors and stdout lines do not", async () => {
    const setup = await render(
      part({
        status: "ok",
        lines: [
          { text: "all good", stream: "stdout" },
          { text: "it broke", stream: "stderr" },
        ],
      }),
    )
    await setup.mockMouse.click(8, 1)
    await setup.flush()
    await setup.waitForVisualIdle()
    const spans = setup.captureSpans().lines.flatMap((line) => line.spans)
    const pick = (text: string) => spans.find((span) => span.text.includes(text))
    expect(pick("all good")).toBeDefined()
    expect(pick("it broke")).toBeDefined()
    expect(pick("it broke")!.fg).not.toEqual(pick("all good")!.fg)
    setup.renderer.destroy()
  })

  test("a click opens the tail and a second click closes it", async () => {
    const setup = await render(
      part({
        status: "ok",
        lines: [
          { text: "first line", stream: "stdout" },
          { text: "second line", stream: "stdout" },
          { text: "third line", stream: "stdout" },
        ],
        preview: "third line",
      }),
    )
    const collapsed = setup.captureCharFrame()
    expect(collapsed).not.toContain("first line")

    await setup.mockMouse.click(8, 1)
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(setup.captureCharFrame()).toContain("first line")

    await setup.mockMouse.click(8, 1)
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(setup.captureCharFrame()).not.toContain("first line")
    setup.renderer.destroy()
  })

  test("a dropped head is announced when the tail is opened", async () => {
    const setup = await render(
      part({ status: "ok", lines: [{ text: "kept tail", stream: "stdout" }], linesDropped: true }),
    )
    await setup.mockMouse.click(8, 1)
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(setup.captureCharFrame()).toContain("截断")
    setup.renderer.destroy()
  })
})