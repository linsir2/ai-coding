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
  approvalMs: 0,
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

  test("a settled call names the approval wait (A-10)", async () => {
    const setup = await render(part({ status: "ok", durationMs: 28_900, approvalMs: 28_500 }))
    const frame = setup.captureCharFrame()
    expect(frame).toContain("28.9s（审批 28.5s）")
    setup.renderer.destroy()
  })

  test("a call nobody was asked about shows only its duration", async () => {
    const setup = await render(part({ status: "ok", durationMs: 820 }))
    expect(setup.captureCharFrame()).not.toContain("审批")
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
    const frame = setup.captureCharFrame()
    expect(frame).toContain("被拒绝")
    // R22 A-17: the denial never ran, so the title says it was tried.
    expect(frame).toContain("已尝试编辑文件")
    setup.renderer.destroy()
  })

  // R22 A-17: A-4's real frame read "✗ 已运行命令 · ls" over "沙箱不可用（命令未执行）",
  // which says the command ran and did not run in the same card.
  test("a failed card says it tried, not that it ran", async () => {
    const setup = await render(
      part({ target: "ls", error: "sandbox_unavailable" }),
    )
    const frame = setup.captureCharFrame()
    expect(frame).toContain("已尝试运行命令 · ls")
    expect(frame).toContain("沙箱不可用（命令未执行）")
    expect(frame).not.toContain("已运行命令")
    setup.renderer.destroy()
  })

  // The running branch is covered by toolmeta.test.ts: a running part draws the
  // spinner, which the render harness has no component for.


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

  // 20 §批次2: the todo card was the raw-task-state wall.  The plan part is
  // the single place the list lives now -- a settled call draws nothing, a
  // failure still draws (it is the only place that reaches the operator).
  test("a settled todo_write draws nothing at all", async () => {
    const setup = await render(
      part({
        toolName: "todo_write",
        verb: "更新计划",
        target: "",
        status: "ok",
        preview: "<task-state>\n(无任务项)\n</task-state>",
      }),
    )
    // An empty frame still carries the renderer's filler glyphs, so emptiness
    // is asserted on the content that used to draw, not on whitespace.
    const frame = setup.captureCharFrame()
    expect(frame).not.toContain("更新计划")
    expect(frame).not.toContain("<task-state>")
    setup.renderer.destroy()
  })

  test("a failed todo_write still draws its card", async () => {
    const setup = await render(
      part({ toolName: "todo_write", verb: "更新计划", target: "", error: "guard_denied" }),
    )
    const frame = setup.captureCharFrame()
    expect(frame).toContain("更新计划")
    expect(frame).toContain("门卫拦截")
    setup.renderer.destroy()
  })

  // 23: the research conclusion is drawn as prose (the conclusion event, and
  // the persisted tool result on reload), so its card would only repeat the
  // first 120 chars under a "ran a tool" heading -- the "filed under a
  // category" the user reported.  A refusal still draws: it is the only place
  // that reaches the operator.
  test("a settled finish_research draws no card", async () => {
    const setup = await render(
      part({
        toolName: "finish_research",
        verb: "运行工具",
        target: "finish_research",
        status: "ok",
        preview: "## 调研结论：会话落盘机制 本仓库不包含会话持久化的源代码实现",
      }),
    )
    const frame = setup.captureCharFrame()
    expect(frame).not.toContain("finish_research")
    expect(frame).not.toContain("调研结论")
    setup.renderer.destroy()
  })

  test("a failed finish_research still draws its card", async () => {
    const setup = await render(
      part({
        toolName: "finish_research",
        verb: "运行工具",
        target: "finish_research",
        error: "implementation_request",
      }),
    )
    const frame = setup.captureCharFrame()
    expect(frame).toContain("finish_research")
    expect(frame).toContain("该请求要实现")
    setup.renderer.destroy()
  })

  test("a submitted plan opens into the plan that was actually sent", async () => {
    // M3 T11: `task.items` is the run's state and changes; the call's own
    // arguments are what the human approved.  The card must show those.
    const setup = await render(
      part({
        toolName: "present_plan",
        verb: "提交计划",
        target: "",
        status: "ok",
        params: {
          plan: "# 编排层计划\n正文内容",
          items: [{ content: "建目录", verify: "test -d hotel" }],
        },
      }),
    )
    expect(setup.captureCharFrame()).toContain("已提交计划")
    // The card is collapsed until opened; the click path is the row itself
    // (no keyboard binding in this component), so press it through the
    // renderer's mouse helper.
    await setup.mockMouse.click(8, 1)
    await setup.flush()
    await setup.waitForVisualIdle()
    const frame = setup.captureCharFrame()
    expect(frame).toContain("编排层计划")
    // Markdown, not the raw source: a heading must lose its ``#``.
    expect(frame).not.toContain("# 编排层计划")
    expect(frame).toContain("建目录")
    expect(frame).toContain("test -d hotel")
    setup.renderer.destroy()
  })
})
