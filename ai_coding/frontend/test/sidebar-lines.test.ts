/**
 * M4b: the session card's pure lines — sessions facts, budget, job rows.
 *
 * The sidebar's second consumer of these facts (after the transcript) is the
 * session card: turns/messages/start time were never on screen, the budget is
 * the same summary /budget renders, and the job block lists only what is
 * running (settled rows are the panel's business — a permanent "0 running"
 * line was the furniture this replaces).
 */

import { describe, expect, test } from "bun:test"
import { budgetLine, JOB_HINT, jobLines, sessionLine, startedLine } from "../src/component/sidebar"
import { toJobRow } from "../src/state/session"

const ROW = {
  id: "job_ab12cd34",
  command: "npm test -- --watch",
  status: "running",
  pid: 4242,
  log_path: "/tmp/ws/.ai-coding/jobs/job_ab12cd34.log",
  detail: "",
  adopted: false,
  startedAt: new Date(2026, 0, 1, 13, 46).toISOString(),
}

describe("sessionLine", () => {
  test("counts turns and message rows", () => {
    expect(sessionLine(12, 34)).toBe("12 轮 · 34 条消息")
  })

  test("a session with no turn yet says so instead of showing 0 轮", () => {
    expect(sessionLine(0, 3)).toBe("尚无回合 · 3 条消息")
  })
})

describe("startedLine", () => {
  test("local clock and elapsed, computed from the injected now", () => {
    const created = new Date(2026, 0, 1, 13, 45).toISOString()
    expect(startedLine(created, new Date(2026, 0, 1, 14, 11))).toBe(
      "13:45 起 · 已 26 分钟",
    )
  })

  test("boundaries: just started, the hour edge, hours with minutes", () => {
    const created = new Date(2026, 0, 1, 13, 0).toISOString()
    expect(startedLine(created, new Date(2026, 0, 1, 13, 0))).toBe("13:00 起 · 刚刚开始")
    expect(startedLine(created, new Date(2026, 0, 1, 14, 0))).toBe("13:00 起 · 已 1 小时")
    expect(startedLine(created, new Date(2026, 0, 1, 14, 30))).toBe(
      "13:00 起 · 已 1 小时 30 分",
    )
  })

  test("an unparseable stamp hides the line", () => {
    expect(startedLine("nope", new Date())).toBe("")
    expect(startedLine("", new Date())).toBe("")
  })
})

describe("budgetLine", () => {
  test("cumulative cost plus the cache share when the provider reported one", () => {
    expect(
      budgetLine({ tokens: 2500, cost: 0.4242, cachedPercent: 76, cachedPriceKnown: true }),
    ).toBe("累计 ¥0.42 · 缓存 76%")
  })

  test("says when cache hits were charged at the input price", () => {
    expect(
      budgetLine({ tokens: 2500, cost: 0.4242, cachedPercent: 76, cachedPriceKnown: false }),
    ).toBe("累计 ¥0.42 · 缓存 76%（缓存按输入价）")
  })

  test("cost only when no cache figure exists, null when unpriced", () => {
    expect(budgetLine({ tokens: 2500, cost: 2, cachedPercent: null })).toBe("累计 ¥2.00")
    expect(budgetLine({ tokens: 2500, cost: null, cachedPercent: 76 })).toBeNull()
    expect(budgetLine(null)).toBeNull()
    expect(budgetLine(undefined)).toBeNull()
  })
})

describe("jobLines", () => {
  test("one line per running job: id prefix, clipped command, start clock", () => {
    expect(jobLines([toJobRow(ROW)])).toEqual(["job_ab12cd34 npm test -- --wa… · 起于 13:46"])
  })

  test("settled jobs do not appear -- the panel keeps the history", () => {
    expect(jobLines([toJobRow({ ...ROW, status: "completed" })])).toEqual([])
    expect(jobLines([])).toEqual([])
  })

  test("more than three running collapse into a count", () => {
    const rows = [1, 2, 3, 4, 5].map((n) =>
      toJobRow({ ...ROW, id: `job_00000000${n}0` }),
    )
    const lines = jobLines(rows)
    expect(lines).toHaveLength(4)
    expect(lines[3]).toBe("…还有 2 个运行中")
  })

  test("a row without a start stamp still renders without the clock", () => {
    const { startedAt: _drop, ...noStamp } = ROW
    expect(jobLines([toJobRow(noStamp)])).toEqual(["job_ab12cd34 npm test -- --wa…"])
  })

  test("the hint names both keys", () => {
    expect(JOB_HINT).toContain("查看")
    expect(JOB_HINT).toContain("停止")
  })
})
