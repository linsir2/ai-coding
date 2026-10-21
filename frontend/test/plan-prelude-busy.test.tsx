/**
 * D7 (wired): the Home + shift+tab submission must read busy until the
 * prompt's turn ends.
 *
 * The route sends `/plan` first, waits for its ack, then submits the prompt.
 * The command's terminal event is emitted by the command runner and can land
 * before the prompt's ack -- the worst ordering, and the one reproduced here.
 * The composer must stay busy through it: an idle composer invites a second
 * submit, which `submit()` silently turns into a steer.
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

/** A client that holds the two turn-starting calls' acks until told. */
class _GatedClient {
  readonly sent: { method: string; params: Record<string, unknown> }[] = []
  private listeners = new Set<(event: EventMessage) => void>()
  private waiting = new Map<string, ((value: unknown) => void)[]>()

  onEvent(listener: (event: EventMessage) => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.sent.push({ method, params })
    if (method === "session.command" || method === "session.prompt") {
      return new Promise((resolve) => {
        const queue = this.waiting.get(method) ?? []
        queue.push(resolve)
        this.waiting.set(method, queue)
      })
    }
    return Promise.resolve({})
  }

  ack(method: string, value: unknown): void {
    const queue = this.waiting.get(method)
    if (!queue?.length) throw new Error(`no pending ${method}`)
    queue.shift()!(value)
  }

  emit(type: string, data: Record<string, unknown>): void {
    for (const listener of [...this.listeners]) listener({ type, data })
  }
}

async function _mount() {
  const base = await createTestRenderer({ width: 120, height: 40 })
  const keymap = setupKeymap(base.renderer)
  const client = new _GatedClient()
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
              initialPrompt={{ text: "看这个", images: [], mode: "plan" }}
              permission={null}
              onPermissionDecide={() => {}}
              ask={null}
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
  return { base, client, setup }
}

describe("prelude turn ownership (D7)", () => {
  test("the command's ending does not free the composer the prompt just took", async () => {
    const { base, client, setup } = await _mount()
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")
    const turnCalls = () =>
      client.sent
        .map((row) => row.method)
        .filter((method) => method === "session.command" || method === "session.prompt")

    // the posture command goes first and holds its ack
    expect(turnCalls()).toEqual(["session.command"])
    client.ack("session.command", { turnId: 1, sessionId: "s1" })
    await setup.flush()
    expect(turnCalls()).toEqual(["session.command", "session.prompt"])
    expect(frameOf()).toContain("working")

    // the command's ending lands before the prompt's ack: still the prompt's turn
    client.emit("turn.finished", { turnId: 1, status: "completed", action: "continue" })
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("working")

    // the prompt's ack names the turn that owns the composer now
    client.ack("session.prompt", { turnId: 2, sessionId: "s1" })
    await setup.flush()
    expect(frameOf()).toContain("working")

    // and only its own ending frees it
    client.emit("turn.finished", { turnId: 2, status: "completed" })
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).not.toContain("working")

    setup.renderer.destroy()
  })
})
