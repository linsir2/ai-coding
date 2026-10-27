/**
 * TDD (M1b T1b): the replayed switch timeline.
 *
 * A switch is committed at a turn boundary and anchored by timestamp, not by
 * a message index: /compact rewrites the message list, so an index would
 * point at the wrong message afterwards. The line lands in the assistant
 * bubble that was already on screen, which keeps the replay's
 * one-bubble-per-turn merge intact.
 */

import { describe, expect, test } from "bun:test"
import { messagesToState } from "../src/state/replay"

const turn = (stamp: string, text: string) => [
  { role: "user", content: `ask ${text}`, timestamp: stamp },
  { role: "assistant", content: text, timestamp: stamp },
]

const noticesOf = (state: ReturnType<typeof messagesToState>) =>
  state.messages.flatMap((m) =>
    m.parts.filter((p) => p.kind === "notice").map((p) => (p as { text: string }).text),
  )

describe("agent history in replay", () => {
  test("a switch is drawn after the turn it happened at", () => {
    const state = messagesToState(
      [...turn("2026-10-22T10:00:00Z", "one"), ...turn("2026-10-22T10:05:00Z", "two")],
      [{ from: "build", to: "plan", ts: "2026-10-22T10:02:00Z", reason: "/plan" }],
    )

    expect(noticesOf(state)).toEqual(["Switched agent: build → plan（/plan）"])
    const first = state.messages.filter((m) => m.role === "assistant")[0]!
    expect(first.parts.some((p) => p.kind === "notice")).toBe(true)
  })

  test("a switch newer than every message still shows, at the end", () => {
    const state = messagesToState(turn("2026-10-22T10:00:00Z", "one"), [
      { from: "plan", to: "build", ts: "2026-10-22T11:00:00Z", reason: "/plan off" },
    ])
    expect(noticesOf(state)).toHaveLength(1)
  })

  test("the switch survives a compacted history", () => {
    const state = messagesToState(turn("2026-10-22T12:00:00Z", "after"), [
      { from: "build", to: "plan", ts: "2026-10-22T10:02:00Z", reason: "/plan" },
    ])
    expect(noticesOf(state)).toHaveLength(1)
  })

  test("an assistant message keeps the agent that produced it", () => {
    const state = messagesToState([
      { role: "user", content: "hi", timestamp: "t1" },
      { role: "assistant", content: "hello", timestamp: "t2", agent: "plan" },
    ])
    expect(state.messages[1]!.agent).toBe("plan")
  })

  test("an un-attributed message has no agent", () => {
    const state = messagesToState([
      { role: "user", content: "hi", timestamp: "t1" },
      { role: "assistant", content: "hello", timestamp: "t2" },
    ])
    expect(state.messages[1]!.agent).toBeUndefined()
  })
})
