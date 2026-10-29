/**
 * W2/R11: the auto-run dispatch, wired.
 *
 * The pure function (util/plan-run.ts) decides *whether* a run may be
 * dispatched; this test drives the route that uses it, because a decision
 * nobody calls is not a decision.  The client here is a stand-in that records
 * what the route asked the backend to do.
 *
 * The scenario: the human approved a plan (the review card's answer), so the
 * route is holding a pending run.  Then the turn that carried the approval
 * ends -- cleanly, or not.  A clean end starts the run; any other ending
 * leaves the session somewhere the plan was not written against, and the run
 * must not start from it.
 *
 * The approval is delivered the way a human delivers it: the review card's
 * keymap command, so the route's own wiring (the card's onAnswer -> the
 * pending flag) is part of what is under test.
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogProvider } from "../src/dialog/context"
import { Session } from "../src/routes/session"
import { setupKeymap } from "../src/keymap"
import { ToastProvider } from "../src/component/toast"
import type { EventMessage, IpcClient } from "../src/ipc"

/** A client that records the commands the route dispatched. */
class _RecordingClient {
  readonly sent: { method: string; params: Record<string, unknown> }[] = []
  private listeners = new Set<(event: EventMessage) => void>()

  onEvent(listener: (event: EventMessage) => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.sent.push({ method, params })
    return {}
  }

  emit(type: string, data: Record<string, unknown>): void {
    for (const listener of [...this.listeners]) listener({ type, data })
  }

  commands(): { method: string; params: Record<string, unknown> }[] {
    return this.sent.filter((c) => c.method === "session.command")
  }
}

/** The review card the backend sends when the model submits a plan. */
const PLAN_REVIEW = {
  question: "批准这个计划并开始执行？",
  options: ["开始执行", "继续规划"],
  detail: "# plan",
  intent: "plan_review",
  data: { items: [{ content: "写代码", verify: "", dependsOn: [] }] },
}

async function _mount() {
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
              ask={PLAN_REVIEW}
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

describe("plan auto-run dispatch (W2/R11)", () => {
  test("an approved plan runs when its turn ends cleanly", async () => {
    const { base, client, keymap, setup } = await _mount()

    // the human approves the card
    keymap.runCommand("plan-review.approve")
    await setup.flush()
    await setup.waitForVisualIdle()

    client.emit("turn.finished", { status: "completed", reason: "", iterations: 1 })
    await setup.flush()

    const commands = client.commands()
    expect(commands.length).toBe(1)
    expect(commands[0].params["text"]).toBe("/plan run")

    setup.renderer.destroy()
  })

  test("a turn cut by the planning budget still runs its approved plan", async () => {
    const { base, client, keymap, setup } = await _mount()

    keymap.runCommand("plan-review.approve")
    await setup.flush()
    await setup.waitForVisualIdle()

    // r22: a 23-step plan can be approved after the planning budget is gone.
    // The plan exists and the task is armed; refusing the run stranded it.
    client.emit("turn.finished", { status: "max_iterations", reason: "", iterations: 32 })
    await setup.flush()

    const commands = client.commands()
    expect(commands.length).toBe(1)
    expect(commands[0].params["text"]).toBe("/plan run")

    setup.renderer.destroy()
  })

  test("a turn that did not end cleanly does not start the run", async () => {
    const { base, client, keymap, setup } = await _mount()

    keymap.runCommand("plan-review.approve")
    await setup.flush()
    await setup.waitForVisualIdle()

    for (const status of ["aborted", "error", "timeout", "budget", "stopped"]) {
      client.emit("turn.finished", { status, reason: "", iterations: 1 })
      await setup.flush()
    }

    expect(client.commands().length).toBe(0)

    setup.renderer.destroy()
  })

  test("without an approval, no turn ending starts a run", async () => {
    const { base, client, setup } = await _mount()

    client.emit("turn.finished", { status: "completed", reason: "", iterations: 1 })
    await setup.flush()

    expect(client.commands().length).toBe(0)
    setup.renderer.destroy()
  })
})
