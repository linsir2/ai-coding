/**
 * r16/E-1: the Home panel must seed the composer, not just close.
 *
 * The key path (return -> dialog row -> onSelect) is not drivable in this
 * harness (see dialog-mcp-render.test.tsx), so this pins the half that is:
 * Home registers the command WITH a selection handler — the panel it opens
 * says "enter add to prompt" instead of the old read-only "esc close". The
 * seeding itself is verified by the r16 tmux run.
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogProvider } from "../src/dialog/context"
import { Home } from "../src/routes/home"
import { setupKeymap } from "../src/keymap"

describe("Home skills panel", () => {
  test("opening it from Home lands the session-aware panel", async () => {
    const base = await createTestRenderer({ width: 100, height: 40 })
    const keymap = setupKeymap(base.renderer)
    const client = {
      request: async (method: string) =>
        method === "skills.list"
          ? { skills: [{ name: "demo-skill", description: "A demo", phase: "" }] }
          : {},
    }
    const setup = await testRender(
      () => (
        <KeymapProvider keymap={keymap}>
          <DialogProvider renderer={base.renderer}>
            <Home
              workspace="/tmp/ws"
              version="0.1.0"
              model="m"
              provider="p"
              branch=""
              onSubmit={() => {}}
              client={client as never}
            />
          </DialogProvider>
        </KeymapProvider>
      ),
      base,
    )
    await setup.flush()

    const result = keymap.runCommand("dialog.skills.show")
    await setup.flush()
    await setup.waitForVisualIdle()
    const frame = setup.captureCharFrame().split("\n").join(" ")
    setup.renderer.destroy()

    expect(result.ok).toBe(true)
    expect(frame).toContain("enter add to prompt")
    expect(frame).not.toContain("esc close")
  })
})
