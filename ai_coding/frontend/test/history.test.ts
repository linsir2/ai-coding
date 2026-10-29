import { describe, expect, test } from "bun:test"

import { historyRewriteTitle, reloadTarget } from "../src/util/history"

/**
 * R22/A-3: the measured frame showed `/undo` as "Compacted 7 → 0 messages".
 * The event's undo payload is the discriminator (it rides only on the commands
 * that rewind), so the helper takes that fact, not the text — an image-only
 * turn has no text and is still an undo.
 */
describe("historyRewriteTitle (R22/A-3)", () => {
  test("a compaction with no rewind says so", () => {
    expect(historyRewriteTitle(false)).toBe("Compacted")
  })

  test("an undo names itself, not the compaction it is not", () => {
    expect(historyRewriteTitle(true)).toBe("Undone")
  })
})

/**
 * R22/A-11: after `/resume <id>` the re-projection asked the server for the
 * session the app had *started* in — an empty one that never reached disk — and
 * the wire answered `session not found`.  The terminal event names the session
 * the command switched to; the projection has to follow it, not the stale prop.
 */
describe("reloadTarget after a terminal event (R22/A-11)", () => {
  test("a switch projects the session the event names", () => {
    expect(reloadTarget("new_session", "new-id", "old-id")).toBe("new-id")
  })

  test("a switch without an id skips the projection instead of repeating the bug", () => {
    // An older backend sends no sessionId; falling back to the prop would ask
    // for the very id that no longer exists.  The route owns that load.
    expect(reloadTarget("new_session", "", "old-id")).toBeNull()
  })

  test("an in-place rewrite (/undo, /redo) stays on this session", () => {
    expect(reloadTarget("reload", "ignored-id", "this-id")).toBe("this-id")
  })

  test("an ordinary command never changes the projection target", () => {
    expect(reloadTarget("continue", "event-id", "this-id")).toBe("this-id")
  })
})
