/**
 * D4: a card takes the keyboard, not the composer.
 *
 * The permission / plan-review panel used to unmount the Prompt: the draft
 * died with the renderable, the layout jumped, and the session's whole
 * binding layer went dark (shift+tab included).  The composer now stays
 * mounted and paused; the card's own keys keep priority.
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

class _RecordingClient {
  readonly sent: { method: string; params: Record<string, unknown> }[] = []

  onEvent(_listener: (event: EventMessage) => void): () => void {
    return () => {}
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.sent.push({ method, params })
    return {}
  }

  commands(): { method: string; params: Record<string, unknown> }[] {
    return this.sent.filter((row) => row.method === "session.command")
  }
}

const PLAN_REVIEW: AskRequest = {
  question: "批准这个计划并开始执行？",
  options: ["开始执行", "继续规划"],
  detail: "# plan",
  intent: "plan_review",
  data: { items: [{ content: "写代码", verify: "", dependsOn: [] }] },
}

async function _mount(card: () => AskRequest | null) {
  const base = await createTestRenderer({ width: 120, height: 40 })
  const keymap = setupKeymap(base.renderer)
  const client = new _RecordingClient()
  const setup = await testRender(
    () => (
      <KeymapProvider keymap={keymap}>
        <ToastProvider>
          <DialogProvider renderer={base.renderer}>
            <Session
              client={client as unknown as IpcClient}
              sessionId="s1"
              model="m"
              provider="p"
              commands={["plan"]}
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
  test("the composer stays and shift+tab still cycles the posture", async () => {
    const card = createSignal<AskRequest | null>(PLAN_REVIEW)
    const { base, client, keymap, setup } = await _mount(() => card[0]())
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")

    // the card is up and the composer is still part of the screen
    expect(frameOf()).toContain("批准这个计划并开始执行？")
    expect(frameOf()).toContain("p · m")
    expect(keymap.getActiveKeys().map((key) => key.display)).toContain("shift+tab")

    // and the posture toggle works through the card, without touching the
    // composer or the card's own keys
    keymap.runCommand("session.mode.cycle.card")
    await setup.flush()
    expect(client.commands().map((row) => row.params["text"])).toEqual(["/plan"])

    // closing the card leaves the composer where it was
    card[1](null)
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("p · m")
    expect(keymap.getActiveKeys().map((key) => key.display)).toContain("shift+tab")

    setup.renderer.destroy()
  })
})
