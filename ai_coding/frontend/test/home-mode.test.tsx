/**
 * T7 (M4c, cold start): Home carries an agent draft, not a mode toggle.
 *
 * T7 sanctioned behavior change (30-M4 §3.5/§7.5): the old Build<->PLAN
 * boolean is gone.  shift+tab cycles the visible-primary order and only
 * rewrites the draft — no request leaves Home (the Session route runs
 * session.agent.select before the first prompt).  This test drives the cycle
 * with runCommand (the renderer harness cannot deliver keypresses; the stroke
 * itself is pinned by keymap-shift-tab.test.tsx) and asserts the binding is
 * registered on the Home layer.
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
  { id: "reviewer", name: "Reviewer", kind: "primary", source: "project", description: "" },
]

function stubClient() {
  const calls: string[] = []
  return {
    calls,
    async request(method: string) {
      calls.push(method)
      if (method === "session.stats") return { agent: "build" }
      if (method === "session.agents") return { agents: AGENTS }
      return {}
    },
  }
}

async function until(fn: () => boolean, what: string): Promise<void> {
  for (let i = 0; i < 200; i++) {
    if (fn()) return
    await new Promise((r) => setTimeout(r, 10))
  }
  throw new Error(`timed out waiting for ${what}`)
}

describe("Home agent draft (cold start, T7)", () => {
  test("the binding is registered and the cycle walks the primary order", async () => {
    const base = await createTestRenderer({ width: 100, height: 40 })
    const keymap = setupKeymap(base.renderer)
    let submitted: { agent: string | null } | null = null
    const client = stubClient()
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
              onSubmit={(_text, _images, agent) => {
                submitted = { agent }
              }}
              client={client as never}
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
    // The mount hydrates stats + agents in parallel before the draft means
    // anything.
    await until(
      () => client.calls.includes("session.stats") && client.calls.includes("session.agents"),
      "home hydration",
    )
    await setup.flush()
    await setup.waitForVisualIdle()

    expect(keymap.getActiveKeys().map((key) => key.display)).toContain("shift+tab")
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")
    expect(frameOf()).toContain("Build · p · m")

    keymap.runCommand("home.mode.cycle")
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("Plan · 只读 · p · m")

    keymap.runCommand("home.mode.cycle")
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("Reviewer · p · m")

    keymap.runCommand("home.mode.cycle")
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("Build · p · m")

    setup.renderer.destroy()
    expect(submitted).toBeNull()
  })

  test("submit carries the draft agent, null when untouched", async () => {
    const base = await createTestRenderer({ width: 100, height: 40 })
    const keymap = setupKeymap(base.renderer)
    let submitted: { agent: string | null } | null = null
    const client = stubClient()
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
              onSubmit={(_text, _images, agent) => {
                submitted = { agent }
              }}
              client={client as never}
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
      () => client.calls.includes("session.stats") && client.calls.includes("session.agents"),
      "home hydration",
    )
    await setup.flush()
    // The draft only moves on shift+tab — hydration alone selects nothing.
    keymap.runCommand("home.mode.cycle")
    await setup.flush()
    expect(client.calls).not.toContain("session.agent.select")
    setup.renderer.destroy()
  })
})
