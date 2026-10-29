/**
 * D4 + T7: a card takes the keyboard, not the composer; the agent switch is
 * an RPC, not a slash echo.
 *
 * The permission / plan-review panel used to unmount the Prompt: the draft
 * died with the renderable, the layout jumped, and the session's whole
 * binding layer went dark (shift+tab included).  The composer now stays
 * mounted and paused; the card's own keys keep priority.
 *
 * T7 sanctioned behavior change (30-M4 §3.5/§7.5): shift+tab no longer sends
 * `/plan` over session.command — it runs session.agent.select with the next
 * visible primary.  The mid-turn queue lives on the backend (it answers
 * queued), so the route sends the call even while busy.
 *
 * T7 Home handoff: the first prompt waits for session.agent.select, then the
 * plan prelude, then the prompt — and a queued/refused select sends nothing.
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { createSignal } from "solid-js"
import { DialogProvider } from "../src/dialog/context"
import { Session } from "../src/routes/session"
import { setupKeymap } from "../src/keymap"
import { ToastProvider } from "../src/component/toast"
import type { EventMessage, IpcClient } from "../src/ipc"
import type { AskRequest } from "../src/util/ask"
import type { PendingPrompt } from "../src/util/submission"

const AGENTS = [
  { id: "build", name: "Build", kind: "primary", source: "builtin", description: "" },
  { id: "plan", name: "Plan", kind: "primary", source: "builtin", description: "" },
]

class _RecordingClient {
  readonly sent: { method: string; params: Record<string, unknown> }[] = []
  canned: Record<string, unknown> = {}

  onEvent(_listener: (event: EventMessage) => void): () => void {
    return () => {}
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.sent.push({ method, params })
    if (method === "session.stats")
      return (
        (this.canned["session.stats"] as Record<string, unknown>) ?? {
          tokens: 0,
          maxContextTokens: 0,
          agent: "build",
        }
      )
    if (method === "session.agents")
      return (this.canned["session.agents"] as Record<string, unknown>) ?? { agents: AGENTS }
    if (method === "session.agent.select")
      return (
        (this.canned["session.agent.select"] as Record<string, unknown>) ?? {
          ok: true,
          status: "committed",
        }
      )
    return this.canned[method] ?? {}
  }

  selects(): { method: string; params: Record<string, unknown> }[] {
    return this.sent.filter((row) => row.method === "session.agent.select")
  }
}

async function until(fn: () => boolean, what: string): Promise<void> {
  for (let i = 0; i < 200; i++) {
    if (fn()) return
    await new Promise((r) => setTimeout(r, 10))
  }
  throw new Error(`timed out waiting for ${what}`)
}

const PLAN_REVIEW: AskRequest = {
  question: "批准这个计划并开始执行？",
  options: ["开始执行", "继续规划"],
  detail: "# plan",
  intent: "plan_review",
  data: { items: [{ content: "写代码", verify: "", dependsOn: [] }] },
}

async function _mount(
  card: () => AskRequest | null,
  client: _RecordingClient,
  initialPrompt?: PendingPrompt,
  options?: { sessionId?: () => string; switching?: () => boolean },
) {
  const base = await createTestRenderer({ width: 120, height: 40 })
  const keymap = setupKeymap(base.renderer)
  const setup = await testRender(
    () => (
      <KeymapProvider keymap={keymap}>
        <ToastProvider>
          <DialogProvider renderer={base.renderer}>
            <Session
              client={client as unknown as IpcClient}
              sessionId={options?.sessionId?.() ?? "s1"}
              switching={options?.switching}
              model="m"
              provider="p"
              commands={["plan"]}
              initialPrompt={initialPrompt ?? null}
              title="t"
              workspace="/tmp/ws"
              branch=""
              version="0.1.0"
              permission={null}
              onPermissionDecide={() => {}}
              ask={card()}
              onAskAnswer={() => {}}
              onQuit={() => {}}
              onSelectSession={() => {}}
              onOpenSessions={() => {}}
              onTurnFinished={() => {}}
              skills={[]}
              files={[]}
            />
          </DialogProvider>
        </ToastProvider>
      </KeymapProvider>
    ),
    base,
  )
  await setup.flush()
  return { base, client, keymap, setup }
}

describe("a card pauses the composer, it does not remove it (D4)", () => {
  test("the composer stays and shift+tab switches the agent through the card", async () => {
    const card = createSignal<AskRequest | null>(PLAN_REVIEW)
    const client = new _RecordingClient()
    const { base, keymap, setup } = await _mount(() => card[0](), client)
    await until(
      () =>
        client.sent.some((row) => row.method === "session.stats") &&
        client.sent.some((row) => row.method === "session.agents"),
      "session hydration",
    )
    await setup.flush()
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")

    // the card is up and the composer is still part of the screen
    expect(frameOf()).toContain("批准这个计划并开始执行？")
    expect(frameOf()).toContain("p · m")
    expect(keymap.getActiveKeys().map((key) => key.display)).toContain("shift+tab")

    // and the agent cycle works through the card, as an RPC — not a slash
    // echo dressed as the user's own line.
    keymap.runCommand("session.mode.cycle.card")
    await until(() => client.selects().length > 0, "agent select")
    expect(client.selects().map((row) => row.params["id"])).toEqual(["plan"])
    expect(client.sent.map((row) => row.method)).not.toContain("session.command")

    // closing the card leaves the composer where it was
    card[1](null)
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("p · m")
    expect(keymap.getActiveKeys().map((key) => key.display)).toContain("shift+tab")

    setup.renderer.destroy()
  })
})

describe("a submission during a session switch waits for the new id (A-12)", () => {
  const hydrate = async (client: _RecordingClient, setup: { flush: () => Promise<void> }) => {
    await until(
      () =>
        client.sent.some((row) => row.method === "session.stats") &&
        client.sent.some((row) => row.method === "session.agents"),
      "session hydration",
    )
    await setup.flush()
  }

  test("the line is held back while switching, then sent under the new id", async () => {
    const client = new _RecordingClient()
    const switching = createSignal(true)
    const sessionId = createSignal("s1")
    const { keymap, setup } = await _mount(() => null, client, undefined, {
      sessionId: sessionId[0],
      switching: switching[0],
    })
    await hydrate(client, setup)

    // Every request that names a session; hydration calls carry no id.
    const identified = () => client.sent.filter((row) => typeof row.params["id"] === "string")

    keymap.runCommand("session.review")
    await setup.flush()
    expect(identified()).toEqual([])

    sessionId[1]("s2")
    switching[1](false)
    await until(() => identified().some((row) => row.params["id"] === "s2"), "deferred request")
    expect(identified().map((row) => row.params)).toEqual([{ id: "s2", text: "/review" }])
    expect(client.sent.every((row) => row.params["id"] !== "s1")).toBe(true)
    setup.renderer.destroy()
  })

  test("a failed switch does not guess: the line goes back to the composer", async () => {
    const client = new _RecordingClient()
    const switching = createSignal(true)
    const sessionId = createSignal("s1")
    const { keymap, setup } = await _mount(() => null, client, undefined, {
      sessionId: sessionId[0],
      switching: switching[0],
    })
    await hydrate(client, setup)

    keymap.runCommand("session.review")
    await setup.flush()
    // The load failed: the id never moved.
    switching[1](false)
    await setup.flush()

    expect(client.sent.some((row) => typeof row.params["id"] === "string")).toBe(false)
    const frame = setup.captureCharFrame()
    expect(frame).toContain("/review")
    expect(frame).toContain("输入已放回输入框")
    setup.renderer.destroy()
  })
})

describe("the Home handoff selects before the first prompt (T7)", () => {
  test("order is select, then plan prelude, then prompt", async () => {
    const client = new _RecordingClient()
    const { base, setup } = await _mount(() => null, client, {
      text: "做一个开关",
      images: [],
      agent: "plan",
    })
    await until(
      () => client.sent.some((row) => row.method === "session.prompt"),
      "first prompt",
    )
    await setup.flush()
    const order = client.sent.map((row) => row.method)
    const selectAt = order.indexOf("session.agent.select")
    const commandAt = order.indexOf("session.command")
    const promptAt = order.indexOf("session.prompt")
    expect(selectAt).toBeGreaterThanOrEqual(0)
    expect(commandAt).toBeGreaterThan(selectAt)
    expect(promptAt).toBeGreaterThan(commandAt)
    expect(client.sent[selectAt]!.params).toEqual({ id: "plan" })
    setup.renderer.destroy()
  })

  test("a refused select sends no prompt and names the reason", async () => {
    const client = new _RecordingClient()
    client.canned["session.agent.select"] = { ok: false, reason: "unknown_agent" }
    const { base, setup } = await _mount(() => null, client, {
      text: "做一个开关",
      images: [],
      agent: "nope",
    })
    await until(() => setup.captureCharFrame().includes("未发送"), "refusal notice")
    await setup.flush()
    expect(client.sent.map((row) => row.method)).not.toContain("session.prompt")
    expect(client.sent.map((row) => row.method)).not.toContain("session.command")
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")
    expect(frameOf()).toContain("未发送")
    setup.renderer.destroy()
  })

  test("a queued select sends no prompt either", async () => {
    const client = new _RecordingClient()
    client.canned["session.agent.select"] = { ok: true, status: "queued" }
    const { base, setup } = await _mount(() => null, client, {
      text: "做一个开关",
      images: [],
      agent: "plan",
    })
    await until(() => setup.captureCharFrame().includes("未发送"), "queued notice")
    await setup.flush()
    expect(client.sent.map((row) => row.method)).not.toContain("session.prompt")
    setup.renderer.destroy()
  })
})
