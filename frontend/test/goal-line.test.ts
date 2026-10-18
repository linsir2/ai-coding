import { describe, expect, test } from "bun:test"
import { goalLine } from "../src/component/sidebar"
import type { GoalStatus } from "../src/state/session"

/**
 * G2: the sidebar's one goal line.  Two things can be wrong -- the numbers the
 * wire carries and the words the line spends -- and neither is worth a
 * rendered frame to find out (same rule as jobLine).
 */
const status = (over: Partial<GoalStatus> = {}): GoalStatus => ({
  goal: "迁移 X",
  armed: true,
  roundsRun: 0,
  tokensUsed: 0,
  phase: "",
  ...over,
})

describe("goalLine", () => {
  test("no round yet reads as running, not as round zero", () => {
    expect(goalLine(status())).toBe("目标：进行中")
  })

  test("rounds and spend join the line when there is something to say", () => {
    expect(goalLine(status({ roundsRun: 3, tokensUsed: 24_077 }))).toBe(
      "目标：第 3 轮 · 24,077 tokens",
    )
  })

  test("a round without a reported spend stays one segment", () => {
    expect(goalLine(status({ roundsRun: 1 }))).toBe("目标：第 1 轮")
  })
})
