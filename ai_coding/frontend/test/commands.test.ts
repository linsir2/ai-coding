/**
 * A-9: the command registry view feeds the palette and Help.  A session route
 * registers its own twin of an app command under the same name (Review, Queued
 * input), and the keymap's registered view lists every layer's entries, so both
 * copies reached the palette as two identical rows.  The view de-duplicates by
 * name, keeping the last registration (the route's, which is the one closer to
 * the live session).
 */

import { describe, expect, test } from "bun:test"
import type { CommandRow } from "../src/dialog/commands"
import { dedupeRowsByName } from "../src/dialog/commands"

const row = (name: string, title: string, description?: string): CommandRow => ({
  name,
  title,
  description,
  group: "Session",
  palette: true,
  shortcut: "",
})

describe("dedupeRowsByName", () => {
  test("keeps the last registration of a name", () => {
    const rows = [row("dialog.inbox.show", "Queued input", "app"), row("dialog.inbox.show", "Queued input", "session")]
    expect(dedupeRowsByName(rows)).toEqual([row("dialog.inbox.show", "Queued input", "session")])
  })

  test("keeps the first-seen position, even when the duplicate is not adjacent", () => {
    const rows = [
      row("session.review", "Review"),
      row("dialog.models.show", "Switch model"),
      row("session.review", "Review", "live"),
    ]
    expect(dedupeRowsByName(rows).map((r) => [r.name, r.description])).toEqual([
      ["session.review", "live"],
      ["dialog.models.show", undefined],
    ])
  })

  test("leaves a list with no duplicates as-is", () => {
    const rows = [row("a", "A"), row("b", "B")]
    expect(dedupeRowsByName(rows)).toEqual(rows)
  })
})
