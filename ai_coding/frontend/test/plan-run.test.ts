import { describe, expect, test } from "bun:test"
import { shouldDispatchPlanRun } from "../src/util/plan-run"

describe("shouldDispatchPlanRun", () => {
  test("an approved plan runs when its turn completed", () => {
    expect(shouldDispatchPlanRun(true, "completed")).toBe(true)
  })

  test("an approved plan still runs when the planning budget was the ending", () => {
    // r22: a large plan can be submitted and approved after the planning
    // budget is gone; the plan exists and the task is armed, so refusing the
    // run stranded an approved plan ("approved, then nothing").
    expect(shouldDispatchPlanRun(true, "max_iterations")).toBe(true)
  })

  test("a turn that did not end cleanly never dispatches the run", () => {
    // The remaining terminal statuses of a turn (server/domain/loop.py:16-25).
    // They left the session mid-flight, and dispatching a run on top of that
    // is how a plan ends up executing against a state nobody looked at.
    for (const status of ["aborted", "error", "timeout", "budget", "stopped"]) {
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
