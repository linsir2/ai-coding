import { describe, expect, test } from "bun:test"
import { armDecision, jobRowLine } from "../src/component/dialog-jobs"
import { JOB_HINT, jobLine } from "../src/component/sidebar"
import { initialState, jobsFromEvent, reduce, toJobRow } from "../src/state/session"

/**
 * D-1b: the panel's own data.  Three things can be wrong here -- the rows the
 * wire carries, the one line the sidebar spends, and the words on a row -- and
 * none of them is worth a rendered frame to find out.
 */
const ROW = {
  id: "job_ab12cd34",
  command: "npm test -- --watch",
  status: "running",
  pid: 4242,
  log_path: "/tmp/ws/.ai-coding/jobs/job_ab12cd34.log",
  detail: "",
  adopted: false,
}

describe("job.changed", () => {
  test("a job.changed event replaces the panel rows and writes no transcript line", () => {
    const before = initialState()

    const after = reduce(before, {
      kind: "wire",
      event: { type: "job.changed", data: { jobs: [ROW] } } as never,
    })

    expect(after.jobs).toHaveLength(1)
    expect(after.jobs[0]?.id).toBe("job_ab12cd34")
    // The transcript is untouched: a running command is not something the user
    // said, and folding it in would put words in their conversation.
    expect(after.messages).toHaveLength(0)
  })

  test("a malformed payload leaves the previous rows alone", () => {
    const state = { ...initialState(), jobs: [toJobRow(ROW)] }

    const after = reduce(state, {
      kind: "wire",
      event: { type: "job.changed", data: { jobs: "nope" } } as never,
    })

    expect(after.jobs).toHaveLength(1)
    expect(jobsFromEvent({ jobs: "nope" })).toBeNull()
  })
})

describe("the sidebar line", () => {
  test("counts running and finished separately", () => {
    expect(jobLine([toJobRow(ROW), toJobRow({ ...ROW, id: "job_2", status: "completed" })])).toBe(
      "1 个后台任务运行中 · 1 个已结束",
    )
  })

  test("does not exist when nothing is running", () => {
    // The render side checks the length; this is the contract it checks.
    expect(jobLine([])).toBe("0 个后台任务运行中")
    expect(JOB_HINT).toContain("停止")
  })
})

describe("a row", () => {
  test("leads with the id so the stop command can name it", () => {
    const row = jobRowLine(toJobRow(ROW))

    expect(row.title.startsWith("job_ab12cd34")).toBe(true)
    expect(row.description).toContain("/tmp/ws/.ai-coding/jobs/job_ab12cd34.log")
  })

  test("says when a job belongs to an earlier run", () => {
    const row = jobRowLine(toJobRow({ ...ROW, adopted: true }))

    expect(row.description).toContain("上次运行遗留")
  })

  test("clips a long command rather than wrapping the panel", () => {
    const row = jobRowLine(toJobRow({ ...ROW, command: "x".repeat(200) }))

    expect(row.title.length).toBeLessThan(90)
    expect(row.title.endsWith("…")).toBe(true)
  })
})

describe("arming a stop", () => {
  test("the first press arms, the second one fires, a different row re-arms", () => {
    expect(armDecision("", "job_1")).toBe("arm")
    expect(armDecision("job_1", "job_1")).toBe("fire")
    // Moving to another row must arm it rather than stop the previous one.
    expect(armDecision("job_1", "job_2")).toBe("arm")
  })
})
