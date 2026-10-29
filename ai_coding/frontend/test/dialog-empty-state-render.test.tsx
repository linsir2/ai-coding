/**
 * r22/A-6: every list panel says why it is empty, in Chinese, and does not
 * leave the generic "no matches" line to stand in for "there is nothing here".
 *
 * DialogSelect makes the decision (empty list vs a query that matched nothing);
 * util/dialog-select.test.ts pins the wording, and these frames pin that it
 * reaches the screen and that each panel passes its own sentence.
 *
 * Each case renders its own renderer and destroys it before returning, the
 * same as dialog-mcp-render.test.tsx: the capture is the list's visible
 * height, so a shared renderer lets one case bleed into the next.
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import type { JSX } from "solid-js"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogSelect } from "../src/component/dialog-select"
import { DialogSkills } from "../src/component/dialog-skills"
import { DialogSessions } from "../src/component/dialog-sessions"
import { DialogProvider } from "../src/dialog/context"
import { setupKeymap } from "../src/keymap"

/** A bare DialogSelect: the component decides only from its options. */
const selectFrame = async (options: unknown[], emptyText?: string): Promise<string> => {
  const base = await createTestRenderer({ width: 100, height: 30 })
  const setup = await testRender(
    () => (
      <KeymapProvider keymap={setupKeymap(base.renderer)}>
        <DialogSelect title="Pick" options={options as never} emptyText={emptyText} />
      </KeymapProvider>
    ),
    base,
  )
  await setup.flush()
  await setup.waitForVisualIdle()
  const text = setup.captureCharFrame()
  setup.renderer.destroy()
  return text
}

/** Panels call useDialog(), so they need the provider the app gives them. */
const panelFrame = async (panel: () => JSX.Element): Promise<string> => {
  const base = await createTestRenderer({ width: 100, height: 30 })
  const setup = await testRender(
    () => (
      <KeymapProvider keymap={setupKeymap(base.renderer)}>
        <DialogProvider renderer={base.renderer}>{panel()}</DialogProvider>
      </KeymapProvider>
    ),
    base,
  )
  await setup.flush()
  await setup.waitForVisualIdle()
  const text = setup.captureCharFrame()
  setup.renderer.destroy()
  return text
}

describe("DialogSelect empty state", () => {
  test("no options says there is nothing to pick, not 'no matches'", async () => {
    const text = await selectFrame([])
    expect(text).toContain("没有可选项")
    expect(text).not.toContain("no matches")
  })

  test("a panel can supply its own sentence", async () => {
    const text = await selectFrame([], "自定义空态")
    expect(text).toContain("自定义空态")
  })
})

describe("panel empty states", () => {
  test("the skills panel says where skills go", async () => {
    const text = await panelFrame(() => (
      <DialogSkills client={{ request: async () => ({ skills: [] }) } as never} onSelect={() => {}} />
    ))
    expect(text).toContain("还没有技能")
    expect(text).toContain("skills/")
  })

  test("the sessions panel says a session starts with a message", async () => {
    const text = await panelFrame(() => (
      <DialogSessions
        client={{ request: async () => ({ sessions: [] }) } as never}
        currentId=""
        onSelect={() => {}}
      />
    ))
    expect(text).toContain("还没有会话")
  })
})
