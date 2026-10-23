/**
 * E1 + T7: the agent switch must be visible before the first submission.
 *
 * Home carries shift+tab because the Session route does not exist yet at
 * cold start (20 §批次1), but nothing on screen said the key existed.  The
 * hint sits under the composer; T7 sanctioned behavior change (30-M4
 * §3.5/§7.5): it names the drafted agent (not 计划模式/退出计划 — the draft
 * is an id now, and the suffix grammar belongs to the session chrome).
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogProvider } from "../src/dialog/context"
import { Home } from "../src/routes/home"
import { setupKeymap } from "../src/keymap"

const AGENTS = [
  { id: "build", name: "Build", kind: "primary", source: "builtin", description: "" },
  { id: "plan", name: "Plan", kind: "primary", source: "builtin", description: "" },
]

async function until(fn: () => boolean, what: string): Promise<void> {
  for (let i = 0; i < 200; i++) {
    if (fn()) return
    await new Promise((r) => setTimeout(r, 10))
  }
  throw new Error(`timed out waiting for ${what}`)
}

describe("Home agent hint (E1, T7)", () => {
  test("the row names shift+tab and follows the draft", async () => {
    const base = await createTestRenderer({ width: 100, height: 40 })
    const keymap = setupKeymap(base.renderer)
    const calls: string[] = []
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
              client={
                {
                  async request(method: string) {
                    calls.push(method)
                    if (method === "session.stats") return { agent: "build" }
                    if (method === "session.agents") return { agents: AGENTS }
                    return {}
                  },
                } as never
              }
              skills={[]}
              files={[]}
              commands={["plan", "help"]}
            />
          </DialogProvider>
        </KeymapProvider>
      ),
      base,
    )
    await setup.flush()
    await until(
      () => calls.includes("session.stats") && calls.includes("session.agents"),
      "home hydration",
    )
    await setup.flush()
    await setup.waitForVisualIdle()
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")
    expect(frameOf()).toContain("shift+tab")
    expect(frameOf()).toContain("Build")

    keymap.runCommand("home.mode.cycle")
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("shift+tab")
    expect(frameOf()).toContain("Plan")

    setup.renderer.destroy()
  })
})
