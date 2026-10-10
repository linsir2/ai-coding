/**
 * TDD (M3): session-list day buckets (util/session-group.ts).
 *
 * The sessions dialog groups by day like OpenCode's DialogSessionList
 * ("Today" first, then ISO dates).  A missing or unparseable timestamp is not
 * invented into a date — it lands in "Earlier".
 */

import { describe, expect, test } from "bun:test"
import { dayCategory } from "../src/util/session-group"

const now = new Date("2026-10-09T15:00:00Z")

describe("dayCategory", () => {
  test("today reads as Today", () => {
    expect(dayCategory("2026-10-09T08:00:00Z", now)).toBe("Today")
  })

  test("earlier days read as their ISO date", () => {
    expect(dayCategory("2026-10-08T23:59:00Z", now)).toBe("2026-10-08")
    expect(dayCategory("2026-01-02T00:00:00Z", now)).toBe("2026-01-02")
  })

  test("missing or unparseable timestamps land in Earlier", () => {
    expect(dayCategory("", now)).toBe("Earlier")
    expect(dayCategory("not a date", now)).toBe("Earlier")
  })
})
