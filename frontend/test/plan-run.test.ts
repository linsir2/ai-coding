import { describe, expect, test } from "bun:test"
import { shouldDispatchPlanRun } from "../src/util/plan-run"

describe("shouldDispatchPlanRun", () => {
  test("only a completed turn dispatches the run", () => {
    expect(shouldDispatchPlanRun(true, "completed")).toBe(true)
  })

  test("a turn that did not end cleanly never dispatches the run", () => {
    // The seven terminal statuses of a turn (server/domain/loop.py:16-25).
    // Only "completed" means the turn reached a clean end; the rest left the
    // session mid-flight, and dispatching a run on top of that is how a plan
    // ends up executing against a state nobody looked at.
    for (const status of [
      "aborted",
      "error",
      "timeout",
      "budget",
      "max_iterations",
      "stopped",
    ]) {
      expect(shouldDispatchPlanRun(true, status)).toBe(false)
    }
  })

  test("a turn nobody approved never dispatches the run", () => {
    expect(shouldDispatchPlanRun(false, "completed")).toBe(false)
  })

  test("an unknown status is not a clean end", () => {
    expect(shouldDispatchPlanRun(true, undefined)).toBe(false)
    expect(shouldDispatchPlanRun(true, null)).toBe(false)
    expect(shouldDispatchPlanRun(true, "")).toBe(false)
    expect(shouldDispatchPlanRun(true, "COMPLETED")).toBe(false)
  })
})
