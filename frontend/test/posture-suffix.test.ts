import { describe, expect, test } from "bun:test"
import { postureSuffix } from "../src/util/plan-agent"

/**
 * postureSuffix is the single source of truth for the agent-line suffix
 * (composer meta + status bar).  The plan agent is already named "Plan",
 * so its own suffix folds the redundant PLAN away; any other agent keeps
 * the full suffix so the posture stays distinguishable.
 */
describe("postureSuffix", () => {
  test("plan agent in plan+readonly keeps only readonly", () => {
    expect(postureSuffix("plan", "plan", true)).toBe("只读")
  })

  test("non-plan agent in plan+readonly keeps the full suffix", () => {
    expect(postureSuffix("reviewer", "plan", true)).toBe("PLAN·只读")
  })

  test("plan agent in plan-only posture needs no suffix", () => {
    expect(postureSuffix("plan", "plan", false)).toBeNull()
  })

  test("non-plan agent in plan-only posture keeps PLAN", () => {
    expect(postureSuffix("reviewer", "plan", false)).toBe("PLAN")
  })

  test("readonly-only always shows the readonly chip", () => {
    expect(postureSuffix("plan", "normal", true)).toBe("只读")
    expect(postureSuffix("build", "normal", true)).toBe("只读")
  })

  test("normal and writable shows no suffix", () => {
    expect(postureSuffix("build", "normal", false)).toBeNull()
    expect(postureSuffix("build", "normal", undefined)).toBeNull()
    expect(postureSuffix(null, undefined, undefined)).toBeNull()
  })
})
