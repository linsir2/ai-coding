/**
 * D7: a terminal event settles the turn it names, not "whatever is running".
 *
 * Home + shift+tab submits `/plan` first and the prompt right after it; the
 * command's terminal can reach the client while the prompt's turn is still
 * running.  Without the turn id the command's ending cleared `busy` and the
 * composer invited typing into a live turn (and a second submit was steered
 * into it instead of starting anything).
 */

import { describe, expect, test } from "bun:test"
import type { Action, SessionState } from "../src/state/session"
import { initialState, reduce } from "../src/state/session"

const wire = (type: string, data: Record<string, unknown> = {}): Action => ({
  kind: "wire",
  event: { type, data },
})

const feed = (state: SessionState, ...actions: Action[]): SessionState =>
  actions.reduce(reduce, state)

describe("turn ownership (D7)", () => {
  test("an ending for a turn this client never started leaves busy alone", () => {
    const state = feed(
      initialState(),
      { kind: "submitted", text: "看这个", images: [] },
      wire("turn.finished", { turnId: 1, status: "completed" }),
    )
    expect(state.busy).toBe(true)
  })

  test("only the registered turn's ending frees the composer", () => {
    const registered = feed(
      initialState(),
      { kind: "submitted", text: "看这个", images: [] },
      wire("turn.finished", { turnId: 1, status: "completed" }),
      { kind: "turn-started", turnId: 2 },
    )
    expect(registered.turnId).toBe(2)
    expect(registered.busy).toBe(true)

    const stillRunning = reduce(registered, wire("turn.finished", { turnId: 1 }))
    expect(stillRunning.busy).toBe(true)
    expect(stillRunning.turnId).toBe(2)

    const done = reduce(registered, wire("turn.finished", { turnId: 2 }))
    expect(done.busy).toBe(false)
    expect(done.turnId).toBeUndefined()
  })

  test("a registration does not resurrect a turn already freed", () => {
    const state = feed(
      initialState(),
      { kind: "command-started" },
      wire("turn.finished", { status: "completed" }),
      { kind: "turn-started", turnId: 7 },
    )
    expect(state.busy).toBe(false)
  })

  test("an ending with no id still settles (mixed versions)", () => {
    const state = feed(
      initialState(),
      { kind: "command-started" },
      wire("turn.finished", { status: "completed" }),
    )
    expect(state.busy).toBe(false)
  })

  test("a steer adds no turn: the running turn keeps its id", () => {
    const state = feed(
      initialState(),
      { kind: "command-started" },
      { kind: "turn-started", turnId: 3 },
      { kind: "submitted", text: "顺便", images: [] },
    )
    expect(state.turnId).toBe(3)
    expect(reduce(state, wire("turn.finished", { turnId: 3 })).busy).toBe(false)
  })
})
