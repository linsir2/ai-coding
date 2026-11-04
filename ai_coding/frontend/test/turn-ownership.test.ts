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

  // U-1: a fast command (/tasks) can end before its ack arrives.  The ending
  // must be remembered and settled when the ack shows up, not dropped.
  test("an ending that arrives before its ack settles on the ack", () => {
    const ending = feed(
      initialState(),
      { kind: "command-started" },
      wire("turn.finished", { turnId: 5, status: "completed" }),
    )
    expect(ending.busy).toBe(true)

    const acked = reduce(ending, { kind: "turn-started", turnId: 5 })
    expect(acked.busy).toBe(false)
    expect(acked.turnId).toBeUndefined()
  })

  test("an older ending does not settle the new turn", () => {
    const state = feed(
      initialState(),
      { kind: "command-started" },
      wire("turn.finished", { turnId: 1, status: "completed" }),
      { kind: "turn-started", turnId: 2 },
    )
    expect(state.busy).toBe(true)
    expect(state.turnId).toBe(2)
    expect(state.orphanTurnId).toBeUndefined()
  })

  test("an orphan ending while idle is ignored", () => {
    const state = reduce(initialState(), wire("turn.finished", { turnId: 5, status: "completed" }))
    expect(state.busy).toBe(false)
    expect(state.orphanTurnId).toBeUndefined()
  })
})

describe("a refused prompt owns no turn", () => {
  test("turn-refused releases a busy latch that named no turn", () => {
    const submitted = reduce(initialState(), { kind: "submitted", text: "看这个" })
    expect(submitted.busy).toBe(true)
    expect(submitted.turnId).toBeUndefined()

    const refused = reduce(submitted, { kind: "turn-refused" })
    expect(refused.busy).toBe(false)
    expect(refused.turnId).toBeUndefined()
    expect(refused.orphanTurnId).toBeUndefined()
  })

  test("an in-flight ack names the running turn, so its later finish lands", () => {
    const submitted = reduce(initialState(), { kind: "submitted", text: "看这个" })
    const owned = reduce(submitted, { kind: "turn-started", turnId: 9 })
    const done = reduce(owned, wire("turn.finished", { turnId: 9, status: "completed" }))
    expect(done.busy).toBe(false)
  })
})

describe("a nested plan turn keeps its owner across the approval card", () => {
  test("the planning finish frees, a refused image re-attaches to the live turn, and the final finish frees", () => {
    let state = reduce(initialState(), { kind: "submitted", text: "看这个" })
    state = reduce(state, { kind: "turn-started", turnId: 4 })
    state = reduce(state, wire("turn.finished", { turnId: 4, status: "completed" }))
    expect(state.busy).toBe(false)

    // The implementation turn is still running.  An image submit sets busy
    // before the ack; the ack names the live turn, so the final finish lands.
    state = reduce(state, { kind: "submitted", text: "再发一张图" })
    state = reduce(state, { kind: "turn-started", turnId: 4 })
    expect(state.busy).toBe(true)
    state = reduce(state, wire("turn.finished", { turnId: 4, status: "completed" }))
    expect(state.busy).toBe(false)
    expect(state.orphanTurnId).toBeUndefined()
  })
})
